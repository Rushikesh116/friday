"""The dashboard's local web server: page, live WebSocket, stock and image endpoints.

Security: it listens on 127.0.0.1 only, and every request must carry a random token
created at launch (embedded in the page it serves). WebSocket connections must also come
from the dashboard's own origin, so a website open in your browser can't drive Friday.
"""

import asyncio
import ipaddress
import json
import secrets
import time
from typing import TYPE_CHECKING, Any

from aiohttp import WSMsgType, web

from . import config, net, research
from .settings import EFFORTS

if TYPE_CHECKING:
    from .core import Friday
    from .sysmon import SysMon

TOKEN = secrets.token_urlsafe(24)
_img_cache: dict[str, tuple[float, bytes, str]] = {}
_stock_cache: dict[str, tuple[float, dict]] = {}


TAILNET = ipaddress.ip_network("100.64.0.0/10")  # Tailscale's address range
REMOTE_NAMES: set[str] = set(config.REMOTE_HOSTS)  # plus the Tailscale IP, added by app.py at startup


def _local_hosts() -> tuple[str, ...]:
    return (f"127.0.0.1:{config.PORT}", f"localhost:{config.PORT}")


def _from_tailnet(request: web.Request) -> bool:
    try:
        return ipaddress.ip_address(request.remote or "") in TAILNET
    except ValueError:
        return False


def _allowed_host(request: web.Request) -> bool:
    host = request.host.lower()
    if host in _local_hosts():
        return True
    name, _, port = host.rpartition(":") if ":" in host else (host, "", "")
    if name not in REMOTE_NAMES:
        return False
    if port == str(config.PORT):  # direct, on the Tailscale IP: must come from a tailnet device
        return _from_tailnet(request)
    # https via `tailscale serve`: the proxy runs on this Mac.
    return port in ("", "443") and request.remote in ("127.0.0.1", "::1")


def _allowed_origin(request: web.Request) -> bool:
    origin = request.headers.get("Origin")
    if origin is None:
        return True  # same-origin navigation / native window
    remote = {f"http://{n}:{config.PORT}" for n in REMOTE_NAMES} | {f"https://{n}" for n in REMOTE_NAMES}
    return origin in {f"http://{h}" for h in _local_hosts()} | remote


@web.middleware
async def host_guard(request: web.Request, handler):
    # Blocks DNS-rebinding: the page must be addressed as this machine, not some other domain.
    if not _allowed_host(request):
        raise web.HTTPMisdirectedRequest(text="wrong host")
    resp = await handler(request)
    if request.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"  # always revalidate, so updates show up immediately
    return resp


def _asset_version() -> str:
    return str(int(max(p.stat().st_mtime for p in config.WEB.iterdir() if p.is_file())))


def make_app(f: "Friday", mon: "SysMon") -> web.Application:
    app = web.Application(client_max_size=2_000_000, middlewares=[host_guard])

    def check_token(request: web.Request) -> None:
        if not secrets.compare_digest(request.query.get("token", ""), TOKEN):
            raise web.HTTPForbidden(text="bad token")

    async def index(request: web.Request) -> web.Response:
        v = _asset_version()
        html = ((config.WEB / "index.html").read_text().replace("__FRIDAY_TOKEN__", TOKEN)
                .replace("connect-src 'self'", f"connect-src 'self' ws://{request.host} wss://{request.host}")
                .replace("/static/app.css", f"/static/app.css?v={v}").replace("/static/app.js", f"/static/app.js?v={v}"))
        return web.Response(text=html, content_type="text/html",
                            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    async def stock(request: web.Request) -> web.Response:
        check_token(request)
        symbol, rng = request.query.get("symbol", ""), request.query.get("range", "1mo")
        key = f"{symbol.upper()}:{rng}"
        hit = _stock_cache.get(key)
        if hit and time.time() - hit[0] < 300:
            return web.json_response(hit[1])
        try:
            data = await research.stock_series(symbol, rng)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=502)
        _stock_cache[key] = (time.time(), data)
        return web.json_response(data)

    async def img(request: web.Request) -> web.Response:
        """Fallback image fetcher for research photos whose sites block direct embedding."""
        check_token(request)
        url = request.query.get("u", "")
        hit = _img_cache.get(url)
        if not hit:
            try:
                body, ctype, _ = await net.fetch(url, max_bytes=8_000_000, accept="image/avif,image/webp,image/*")
            except Exception as e:
                raise web.HTTPBadGateway(text=str(e))
            if not ctype.startswith("image/") or "svg" in ctype:
                raise web.HTTPUnsupportedMediaType(text="not a picture")
            hit = (time.time(), body, ctype.split(";")[0])
            _img_cache[url] = hit
            if len(_img_cache) > 120:
                for k in sorted(_img_cache, key=lambda k: _img_cache[k][0])[:40]:
                    _img_cache.pop(k, None)
        return web.Response(body=hit[1], content_type=hit[2],
                            headers={"Cache-Control": "max-age=86400", "X-Content-Type-Options": "nosniff"})

    async def ws_handler(request: web.Request) -> web.WebSocketResponse:
        check_token(request)
        if not _allowed_origin(request):
            raise web.HTTPForbidden(text="bad origin")
        ws = web.WebSocketResponse(heartbeat=25, max_msg_size=1_000_000)
        await ws.prepare(request)
        await ws.send_str(json.dumps({"type": "snapshot", "data": snapshot(f, mon)}, default=str))
        f.hub.clients.add(ws)
        mon.request_calendar()
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                    reply = await handle(f, mon, data.get("type", ""), data.get("data") or {})
                except Exception as e:
                    reply = {"error": f"{type(e).__name__}: {e}" if not isinstance(e, (ValueError, KeyError))
                             else str(e).strip("'\"")}
                if reply is not None and data.get("rid"):
                    await ws.send_str(json.dumps({"type": "reply", "rid": data["rid"], "data": reply}, default=str))
        finally:
            f.hub.clients.discard(ws)
        return ws

    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/api/stock", stock)
    app.router.add_get("/api/img", img)
    app.router.add_static("/static/", config.WEB, show_index=False)
    return app


def snapshot(f: "Friday", mon: "SysMon") -> dict[str, Any]:
    return {
        **f.hub.snapshot(),
        **mon.snapshot(),
        "schedules": f.scheduler.items,
        "memory": f.memory.items,
        "settings": f.settings.values,
        "voices": [{"name": n, "locale": loc} for n, loc in config.list_voices() if loc.startswith("en")],
        "efforts": EFFORTS,
        "config": {"name": config.NAME, "model": config.MODEL, "max_parallel": config.MAX_PARALLEL},
    }


async def handle(f: "Friday", mon: "SysMon", kind: str, d: dict[str, Any]) -> Any:
    """One command from the dashboard. Returns a reply for the caller (or None)."""
    match kind:
        case "chat":
            f.chat(str(d.get("text", ""))[:8000])
        case "task":
            run = f.tasks.start(str(d.get("prompt", ""))[:8000], kind="task",
                                title=(str(d.get("title", "")).strip() or None), speak=bool(d.get("speak", False)))
            return {"run": run.id}
        case "research":
            run = f.tasks.start_research(str(d.get("topic", "")), speak=bool(d.get("speak", True)))
            return {"run": run.id}
        case "briefing":
            return {"run": f.tasks.briefing().id}
        case "cancel_run":
            rid = str(d.get("id", ""))
            if rid == "conversation":
                f.stop_all()
                return {"ok": True}
            return {"ok": f.tasks.cancel(rid)}
        case "approve":
            return {"ok": f.hub.resolve_approval(str(d.get("id", "")), bool(d.get("ok")), "dashboard")}
        case "power":
            f.set_power(bool(d.get("on")))
        case "listen_mode":
            f.set_listen_mode(str(d.get("mode", "")))
        case "voice_replies":
            f.set_voice_replies(bool(d.get("on")))
        case "talk":
            f.talk_now()
        case "stop":
            f.stop_all()
        case "new_conversation":
            t = await f.new_conversation(str(d.get("title", "")))
            return {"ok": True, "thread": t["id"]}
        case "thread_switch":
            await f.switch_thread(str(d.get("id", "")))
        case "thread_rename":
            f.rename_thread(str(d.get("id", "")), str(d.get("title", "")))
        case "thread_delete":
            await f.delete_thread(str(d.get("id", "")))
        case "threads":
            return {"threads": f.hub.thread_list()}
        case "setting":
            key, value = str(d.get("key", "")), d.get("value")
            if key in ("power", "listen_mode", "voice_replies"):
                raise ValueError("use the dedicated control for that")
            await f.set_setting(key, value)
            if key in ("city", "units"):
                asyncio.create_task(mon.refresh_weather())
            return {"ok": True, "settings": f.settings.values}
        case "speak":
            text = str(d.get("text", ""))[:2000]
            if not f.voice_on:
                raise ValueError("voice is off, so there's nothing to read it with")
            f.mouth.say(text)
        case "preview_voice":
            name = config.resolve_voice(str(d.get("voice", config.VOICE)))
            if not name:
                raise ValueError("that voice isn't installed")
            proc = await asyncio.create_subprocess_exec(
                "say", "-v", name, "-r", str(config.RATE), f"Hi, I'm {config.NAME}. This is how I sound.")
            await proc.wait()
        case "schedule_save":
            sid = d.pop("id", None)
            s = f.scheduler.update(sid, d) if sid else f.scheduler.add(d)
            return {"ok": True, "schedule": s}
        case "schedule_delete":
            f.scheduler.remove(str(d.get("id", "")))
        case "schedule_toggle":
            f.scheduler.update(str(d.get("id", "")), {"enabled": bool(d.get("enabled"))})
        case "schedule_run":
            f.scheduler.run_now(str(d.get("id", "")))
        case "memory_add":
            return {"ok": True, "item": f.memory.add(str(d.get("text", "")))}
        case "memory_delete":
            f.memory.remove(str(d.get("id", "")))
        case "run_detail":
            run = f.hub.runs.get(str(d.get("id", "")))
            return run.to_dict() if run else {"error": "not found"}
        case "timeline":
            mon.request_calendar(force=bool(d.get("force")))
            return mon.timeline()
        case "weather_refresh":
            await mon.refresh_weather()
        case "ping":
            return {"pong": time.time()}
        case _:
            raise ValueError(f"unknown command '{kind}'")
    return {"ok": True}
