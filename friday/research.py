"""The researcher: investigates a topic and publishes an illustrated brief to the dashboard.

Its tools (besides web search and fetch):
  page_images     the lead photos of a news article (what readers saw with the story)
  wiki_lookup     a Wikipedia summary and portrait/logo for a person, company or event
  commons_images  freely licensed photos from Wikimedia Commons
  stock_history   real price history for a ticker, with the biggest daily moves
  publish_brief   hand the finished brief (story, timeline, figures, photos) to the dashboard
"""

import html
import math
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urlencode, urljoin

from claude_agent_sdk import ToolAnnotations, create_sdk_mcp_server, tool

from . import net
from .hub import new_id

RESEARCH_PERSONA = """
# You are {name}'s researcher

You investigate one topic for the user and publish an illustrated brief that pops up on
their dashboard while {name} reads its summary aloud.

## How to research
- Search broadly first (WebSearch), then read the most relevant, reputable articles
  (WebFetch). For news, prefer the original reporting and established outlets, and check
  that dates line up. Note when something happened, not just what.
- Report only what your sources support. Attribute contested or sensational claims to who
  made them ("according to Reuters…", "a lawsuit alleges…") and keep confirmed facts apart
  from allegations and rumours. If sources disagree or a claim is unverified, say so in
  `caveats`. Never invent numbers, quotes or events.
- Pictures: call page_images on the articles you used (their lead photos show the actual
  event), and wiki_lookup for the main people or companies (portraits, logos). Use
  commons_images when you need more. Pick 3 to 6 images that genuinely illustrate the story,
  with an honest caption each. Skip logos of the news sites themselves, ads and icons.
- If a public company is central to the story, call stock_history for a window covering the
  events (for example 1mo, 6mo or 1y) and report the real move, with dates, as figures.
- Use subagents (Task) only if the topic has clearly separate threads to chase in parallel.

## Finishing
Call publish_brief exactly once with the finished brief:
- summary: 2 to 4 plain spoken sentences that answer the question (no markdown; it is read aloud).
- sections: the story in 2 to 5 short sections, plain prose.
- timeline: the key dated events in order. figures: the key numbers with their dates.
- images, sources (every article you relied on), and caveats if any.
After publishing, reply with just the summary text.
"""


class _PageScan(HTMLParser):
    """Collects title, social-preview images and content images from an article page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta_images: list[str] = []
        self.imgs: list[tuple[str, str, int]] = []  # (src, alt, score)
        self._in_title = False
        self._depth = {"article": 0, "figure": 0, "main": 0}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag in self._depth:
            self._depth[tag] += 1
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key in ("og:image", "og:image:url", "og:image:secure_url", "twitter:image", "twitter:image:src"):
                if a.get("content"):
                    self.meta_images.append(a["content"])
        elif tag == "link" and a.get("rel", "").lower() == "image_src" and a.get("href"):
            self.meta_images.append(a["href"])
        elif tag in ("img", "source"):
            src = a.get("src") or a.get("data-src") or a.get("data-original") or ""
            srcset = a.get("srcset") or a.get("data-srcset") or ""
            if srcset:
                best = _largest_from_srcset(srcset)
                if best:
                    src = best
            if not src or src.startswith("data:"):
                return
            score = 0
            try:
                score += min(int(re.sub(r"\D", "", a.get("width", "") or "0") or 0), 2000) // 100
            except ValueError:
                pass
            if self._depth["figure"] or self._depth["article"] or self._depth["main"]:
                score += 5
            self.imgs.append((src, a.get("alt", ""), score))

    def handle_endtag(self, tag: str) -> None:
        if tag in self._depth and self._depth[tag]:
            self._depth[tag] -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title and len(self.title) < 300:
            self.title += data


def _largest_from_srcset(srcset: str) -> str:
    best, best_w = "", -1
    for part in srcset.split(","):
        bits = part.strip().split()
        if not bits:
            continue
        w = 0
        if len(bits) > 1 and bits[1].endswith("w"):
            try:
                w = int(bits[1][:-1])
            except ValueError:
                w = 0
        if w > best_w:
            best, best_w = bits[0], w
    return best


JUNK = re.compile(r"(logo|icon|sprite|avatar|favicon|pixel|spacer|blank|1x1|badge|button|placeholder|"
                  r"tracking|/ads?/|advert|gravatar|emoji)", re.I)


def ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def fail(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"Error: {msg}"}], "is_error": True}


READ_ONLY = ToolAnnotations(readOnlyHint=True)


@tool("page_images",
      "Get the title and lead photos of a web page (usually a news article): its social preview "
      "image plus the main content images, as absolute URLs with their alt text.",
      {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
      annotations=READ_ONLY)
async def page_images(args: dict[str, Any]) -> dict[str, Any]:
    url = str(args.get("url", "")).strip()
    try:
        body, ctype, final = await net.fetch(url, max_bytes=2_500_000, accept="text/html,*/*")
    except Exception as e:
        return fail(str(e))
    if "html" not in ctype and not body.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")):
        return fail(f"not an HTML page ({ctype})")
    scan = _PageScan()
    try:
        scan.feed(body.decode("utf-8", errors="replace"))
    except Exception:
        pass
    seen, out = set(), []
    candidates = [(u, "social preview image", 100) for u in scan.meta_images]
    candidates += sorted(scan.imgs, key=lambda x: -x[2])
    for src, alt, _score in candidates:
        absu = urljoin(final, html.unescape(src.strip()))
        if not absu.startswith(("http://", "https://")) or JUNK.search(absu) or absu in seen:
            continue
        if re.search(r"\.(svg|gif)(\?|$)", absu, re.I):
            continue
        seen.add(absu)
        out.append(f"- {absu}" + (f"  (alt: {alt.strip()[:120]})" if alt.strip() else ""))
        if len(out) >= 8:
            break
    title = " ".join(scan.title.split())[:200]
    if not out:
        return ok(f"Title: {title}\nNo usable images found on this page.")
    return ok(f"Title: {title}\nImages (best first):\n" + "\n".join(out))


@tool("wiki_lookup",
      "Look up a person, company, place or event on Wikipedia: returns the article title, a short "
      "summary, the main image (portrait, logo, photo) URL and the page URL.",
      {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
      annotations=READ_ONLY)
async def wiki_lookup(args: dict[str, Any]) -> dict[str, Any]:
    q = str(args.get("query", "")).strip()
    try:
        found = await net.fetch_json("https://en.wikipedia.org/w/api.php?" + urlencode(
            {"action": "query", "list": "search", "srsearch": q, "srlimit": 1, "format": "json"}))
        hits = found.get("query", {}).get("search", [])
        if not hits:
            return fail(f"no Wikipedia article for '{q}'")
        title = hits[0]["title"]
        s = await net.fetch_json("https://en.wikipedia.org/api/rest_v1/page/summary/" + quote(title.replace(" ", "_")))
    except Exception as e:
        return fail(str(e))
    image = (s.get("originalimage") or s.get("thumbnail") or {}).get("source", "")
    page = s.get("content_urls", {}).get("desktop", {}).get("page", "")
    return ok(f"Title: {s.get('title', title)}\nDescription: {s.get('description', '')}\n"
              f"Image: {image or 'none'}\nPage: {page}\n\n{s.get('extract', '')[:1500]}")


@tool("commons_images",
      "Search Wikimedia Commons for freely licensed photos. Returns image URLs with their title, "
      "licence and author.",
      {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
       "required": ["query"]},
      annotations=READ_ONLY)
async def commons_images(args: dict[str, Any]) -> dict[str, Any]:
    q = str(args.get("query", "")).strip()
    limit = max(1, min(int(args.get("limit", 6) or 6), 10))
    try:
        data = await net.fetch_json("https://commons.wikimedia.org/w/api.php?" + urlencode({
            "action": "query", "generator": "search", "gsrsearch": f"{q} filetype:bitmap", "gsrnamespace": 6,
            "gsrlimit": limit, "prop": "imageinfo", "iiprop": "url|mime|extmetadata", "iiurlwidth": 1200,
            "format": "json"}))
    except Exception as e:
        return fail(str(e))
    pages = sorted((data.get("query", {}).get("pages", {}) or {}).values(), key=lambda p: p.get("index", 0))
    rows = []
    for p in pages:
        info = (p.get("imageinfo") or [{}])[0]
        if info.get("mime") not in ("image/jpeg", "image/png", "image/webp"):
            continue
        meta = info.get("extmetadata", {})
        artist = re.sub(r"<[^>]+>", "", meta.get("Artist", {}).get("value", "")).strip()[:80]
        lic = meta.get("LicenseShortName", {}).get("value", "")
        rows.append(f"- {info.get('thumburl') or info.get('url')}  | {p.get('title', '').removeprefix('File:')}"
                    f" | {lic} | {artist} | page: {info.get('descriptionurl', '')}")
    return ok("\n".join(rows) if rows else f"No Commons photos found for '{q}'.")


RANGES = {"5d": "30m", "1mo": "1d", "3mo": "1d", "6mo": "1d", "1y": "1d", "2y": "1wk", "5y": "1wk"}


async def stock_series(symbol: str, rng: str = "1mo") -> dict[str, Any]:
    """Price history from Yahoo Finance's public chart endpoint."""
    symbol = re.sub(r"[^A-Za-z0-9.\-^=]", "", symbol).upper()[:15]
    if not symbol:
        raise ValueError("no ticker symbol")
    if rng not in RANGES:
        rng = "1mo"
    path = f"/v8/finance/chart/{quote(symbol)}?range={rng}&interval={RANGES[rng]}"
    data, last_err = None, None
    # Yahoo rate-limits full browser user agents but serves a plain one; try both hosts.
    for host in ("query1", "query2"):
        try:
            data = await net.fetch_json(f"https://{host}.finance.yahoo.com{path}", ua="Mozilla/5.0")
            break
        except Exception as e:
            last_err = e
    if data is None:
        if "HTTP 404" in str(last_err):
            raise ValueError(f"no stock found with ticker {symbol}")
        raise ValueError(f"price data unavailable right now ({last_err})")
    res = (data.get("chart", {}).get("result") or [None])[0]
    if not res:
        err = (data.get("chart", {}).get("error") or {}).get("description", "no data")
        raise ValueError(f"{symbol}: {err}")
    ts = res.get("timestamp") or []
    closes = (res.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
    points = [(t, round(c, 4)) for t, c in zip(ts, closes) if c is not None and not math.isnan(c)]
    if len(points) < 2:
        raise ValueError(f"{symbol}: not enough price data")
    meta = res.get("meta", {})
    first, last = points[0][1], points[-1][1]
    moves = []
    for (t0, c0), (t1, c1) in zip(points, points[1:]):
        if c0:
            moves.append((round((c1 - c0) / c0 * 100, 2), t1))
    return {
        "symbol": symbol, "name": meta.get("longName") or meta.get("shortName") or symbol,
        "currency": meta.get("currency", ""), "range": rng, "points": points,
        "first": first, "last": last, "change_pct": round((last - first) / first * 100, 2) if first else 0.0,
        "high": max(c for _, c in points), "low": min(c for _, c in points),
        "biggest_drops": sorted(moves)[:3], "biggest_gains": sorted(moves, reverse=True)[:3],
    }


def _day(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")


@tool("stock_history",
      "Real share-price history for a stock ticker (e.g. NVDA, AAPL, TSLA) over a range "
      "(5d, 1mo, 3mo, 6mo, 1y, 2y, 5y): start/end price, % change, high/low and the biggest "
      "single-period moves with dates.",
      {"type": "object", "properties": {"symbol": {"type": "string"}, "range": {"type": "string"}},
       "required": ["symbol"]},
      annotations=READ_ONLY)
async def stock_history(args: dict[str, Any]) -> dict[str, Any]:
    try:
        s = await stock_series(str(args.get("symbol", "")), str(args.get("range", "1mo")))
    except Exception as e:
        return fail(str(e))
    fmt = lambda moves: ", ".join(f"{pct:+.2f}% on {_day(t)}" for pct, t in moves)  # noqa: E731
    return ok(
        f"{s['name']} ({s['symbol']}), {s['range']}, prices in {s['currency']}\n"
        f"From {s['first']:.2f} on {_day(s['points'][0][0])} to {s['last']:.2f} on {_day(s['points'][-1][0])}: "
        f"{s['change_pct']:+.2f}%\nHigh {s['high']:.2f}, low {s['low']:.2f}\n"
        f"Biggest drops: {fmt(s['biggest_drops'])}\nBiggest gains: {fmt(s['biggest_gains'])}")


BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Headline, e.g. 'What happened at Nvidia this week'"},
        "subtitle": {"type": "string", "description": "One line of context"},
        "summary": {"type": "string", "description": "2-4 plain spoken sentences answering the question"},
        "sections": {"type": "array", "items": {"type": "object", "properties": {
            "heading": {"type": "string"}, "text": {"type": "string"}}, "required": ["heading", "text"]}},
        "timeline": {"type": "array", "items": {"type": "object", "properties": {
            "date": {"type": "string"}, "event": {"type": "string"}}, "required": ["date", "event"]}},
        "figures": {"type": "array", "items": {"type": "object", "properties": {
            "label": {"type": "string"}, "value": {"type": "string"}, "detail": {"type": "string"},
            "trend": {"type": "string", "enum": ["up", "down", "flat"]}}, "required": ["label", "value"]}},
        "images": {"type": "array", "items": {"type": "object", "properties": {
            "url": {"type": "string"}, "caption": {"type": "string"}, "credit": {"type": "string"},
            "source_url": {"type": "string"}}, "required": ["url", "caption"]}},
        "stock": {"type": "object", "properties": {
            "symbol": {"type": "string"}, "range": {"type": "string"}}, "required": ["symbol"]},
        "sources": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "url": {"type": "string"}}, "required": ["title", "url"]}},
        "caveats": {"type": "string", "description": "Disputed, unverified or one-sided claims, if any"},
    },
    "required": ["title", "summary", "sections", "sources"],
}


def _clean_str(v: Any, n: int) -> str:
    return " ".join(str(v or "").split())[:n] if not isinstance(v, (dict, list)) else ""


def _http(u: Any) -> str:
    u = str(u or "").strip()
    return u if re.match(r"^https?://[^\s\"'<>]+$", u) else ""


def normalize_brief(raw: dict[str, Any], topic: str, run_id: str) -> dict[str, Any]:
    """Validate and tidy a brief before it reaches the dashboard (defence in depth: the UI escapes too)."""
    def items(key: str, limit: int) -> list[dict]:
        v = raw.get(key) or []
        return [x for x in v if isinstance(x, dict)][:limit] if isinstance(v, list) else []

    brief = {
        "id": new_id("brief"), "created": time.time(), "topic": topic[:200], "run_id": run_id,
        "title": _clean_str(raw.get("title"), 160) or topic[:160],
        "subtitle": _clean_str(raw.get("subtitle"), 240),
        "summary": _clean_str(raw.get("summary"), 1200),
        "sections": [{"heading": _clean_str(s.get("heading"), 120), "text": str(s.get("text", ""))[:3000].strip()}
                     for s in items("sections", 8) if s.get("text")],
        "timeline": [{"date": _clean_str(t.get("date"), 40), "event": _clean_str(t.get("event"), 300)}
                     for t in items("timeline", 14) if t.get("event")],
        "figures": [{"label": _clean_str(f.get("label"), 60), "value": _clean_str(f.get("value"), 40),
                     "detail": _clean_str(f.get("detail"), 140),
                     "trend": f.get("trend") if f.get("trend") in ("up", "down", "flat") else ""}
                    for f in items("figures", 8) if f.get("value")],
        "images": [{"url": _http(i.get("url")), "caption": _clean_str(i.get("caption"), 220),
                    "credit": _clean_str(i.get("credit"), 120), "source_url": _http(i.get("source_url"))}
                   for i in items("images", 8) if _http(i.get("url"))],
        "sources": [{"title": _clean_str(s.get("title"), 160) or _http(s.get("url")), "url": _http(s.get("url"))}
                    for s in items("sources", 20) if _http(s.get("url"))],
        "caveats": _clean_str(raw.get("caveats"), 800),
        "stock": None,
    }
    st = raw.get("stock")
    if isinstance(st, dict) and re.match(r"^[A-Za-z0-9.\-^=]{1,15}$", str(st.get("symbol", ""))):
        brief["stock"] = {"symbol": str(st["symbol"]).upper(),
                          "range": st.get("range") if st.get("range") in RANGES else "1mo"}
    return brief


def make_server(topic: str, run_id: str, on_brief: Callable[[dict[str, Any]], None]):
    """Research tools for one research run; publish_brief delivers to on_brief."""
    published: list[dict] = []

    @tool("publish_brief", "Publish the finished research brief to the user's dashboard. Call exactly once.",
          BRIEF_SCHEMA)
    async def publish_brief(args: dict[str, Any]) -> dict[str, Any]:
        if published:
            return fail("a brief was already published for this research")
        brief = normalize_brief(args, topic, run_id)
        if not brief["summary"]:
            return fail("summary is required")
        published.append(brief)
        on_brief(brief)
        return ok(f"Published '{brief['title']}' with {len(brief['images'])} images and "
                  f"{len(brief['sources'])} sources. Now reply with just the summary.")

    server = create_sdk_mcp_server(
        "research", tools=[page_images, wiki_lookup, commons_images, stock_history, publish_brief])
    return server, published


TOOL_NAMES = [f"mcp__research__{n}" for n in
              ("page_images", "wiki_lookup", "commons_images", "stock_history", "publish_brief")]
