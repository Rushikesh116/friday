"""Small, careful HTTP fetching for research tools and the dashboard's image proxy.

URLs here come from web pages, so every request (and every redirect) must point at a
public internet address: nothing on localhost or your home network can be reached.
"""

import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import aiohttp

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/18.0 Safari/605.1.15")
# Wikipedia/Wikimedia ask API clients to identify themselves (no personal details here).
API_UA = "FridayDesktopAssistant/1.0 (personal voice assistant; contact via project README)"

_session: aiohttp.ClientSession | None = None


class FetchError(Exception):
    pass


async def session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15, connect=6))
    return _session


async def close() -> None:
    if _session and not _session.closed:
        await _session.close()


async def _is_public(url: str) -> bool:
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(p.hostname, p.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    for info in infos:
        addr = ipaddress.ip_address(info[4][0].split("%")[0])
        if not addr.is_global:
            return False
    return bool(infos)


async def fetch(url: str, max_bytes: int = 3_000_000, ua: str = BROWSER_UA,
                accept: str = "*/*") -> tuple[bytes, str, str]:
    """GET a public URL. Returns (body, content_type, final_url)."""
    s = await session()
    for _ in range(6):  # follow redirects by hand so each hop is checked
        if not await _is_public(url):
            raise FetchError(f"not a public web address: {url}")
        async with s.get(url, allow_redirects=False, headers={"User-Agent": ua, "Accept": accept}) as r:
            if r.status in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                url = urljoin(url, r.headers["Location"])
                continue
            if r.status >= 400:
                raise FetchError(f"HTTP {r.status} from {urlparse(url).hostname}")
            if int(r.headers.get("Content-Length") or 0) > max_bytes:
                raise FetchError("response too large")
            body = bytearray()
            async for chunk in r.content.iter_chunked(64 * 1024):
                body += chunk
                if len(body) > max_bytes:
                    raise FetchError("response too large")
            return bytes(body), r.headers.get("Content-Type", ""), str(r.url)
    raise FetchError("too many redirects")


async def fetch_json(url: str, ua: str = API_UA) -> object:
    import json

    body, _, _ = await fetch(url, max_bytes=5_000_000, ua=ua, accept="application/json")
    return json.loads(body)
