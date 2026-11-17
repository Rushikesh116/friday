"""Live system numbers for the dashboard: CPU, memory, disk, battery, network, weather,
and today's calendar for the mission timeline. Everything shown is real.

Weather is only fetched once you type a city in Settings (Open-Meteo, no account needed).
The connectivity check is a quick TCP connect to Claude's own API host.
"""

import asyncio
import contextlib
import socket
import time
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import psutil

from . import hands, net

if TYPE_CHECKING:
    from .core import Friday

WEATHER_CODES = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 48: "Freezing fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle", 56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains", 80: "Showers", 81: "Showers",
    82: "Heavy showers", 85: "Snow showers", 86: "Snow showers", 95: "Thunderstorm", 96: "Thunderstorm, hail",
    99: "Thunderstorm, hail",
}

TODAY_EVENTS_SCRIPT = f"""
set startD to current date
set time of startD to 0
set endD to startD + 1 * days
set out to ""
tell application "Calendar"
  repeat with c in calendars
    repeat with e in (every event of c whose start date >= startD and start date < endD)
      set out to out & ((start date of e) as «class isot» as string) & "{hands.SEP}" & (summary of e) & "{hands.SEP}" & (name of c) & linefeed
    end repeat
  end repeat
end tell
return out
"""


class SysMon:
    def __init__(self, friday: "Friday"):
        self.f = friday
        self.hub = friday.hub
        self.system: dict = {}
        self.weather: dict = {"status": "unset"}
        self.network: dict = {"online": None, "latency_ms": None}
        self.calendar: dict = {"status": "idle", "events": [], "updated": 0}
        self._geo: tuple[str, dict] | None = None
        self._net_prev: tuple[float, int, int] | None = None
        self._cal_task: asyncio.Task | None = None
        psutil.cpu_percent(None)  # prime the counter

    def snapshot(self) -> dict:
        return {"system": self.system, "weather": self.weather, "network": self.network,
                "timeline": self.timeline()}

    async def run(self) -> None:
        await asyncio.gather(self._system_loop(), self._network_loop(), self._weather_loop())

    # -- system ---------------------------------------------------------------------------
    def _sample(self) -> dict:
        vm = psutil.virtual_memory()
        try:
            disk = psutil.disk_usage("/System/Volumes/Data")
        except OSError:
            disk = psutil.disk_usage("/")
        io = psutil.net_io_counters()
        now = time.monotonic()
        up = down = 0.0
        if self._net_prev:
            dt = max(now - self._net_prev[0], 0.001)
            up = (io.bytes_sent - self._net_prev[1]) / dt
            down = (io.bytes_recv - self._net_prev[2]) / dt
        self._net_prev = (now, io.bytes_sent, io.bytes_recv)
        bat = psutil.sensors_battery()
        return {
            "cpu": round(psutil.cpu_percent(None), 1),
            "cores": psutil.cpu_count(logical=True),
            "ram": round(vm.percent, 1), "ram_used_gb": round(vm.used / 1e9, 1), "ram_total_gb": round(vm.total / 1e9, 1),
            "disk": round(disk.percent, 1), "disk_free_gb": round(disk.free / 1e9, 1),
            "battery": round(bat.percent) if bat else None, "plugged": bat.power_plugged if bat else None,
            "net_up": round(max(up, 0)), "net_down": round(max(down, 0)),
            "uptime_s": int(time.time() - psutil.boot_time()),
        }

    async def _system_loop(self) -> None:
        while True:
            if self.hub.clients:
                self.system = await asyncio.to_thread(self._sample)
                self.hub.emit("system", self.system)
            await asyncio.sleep(2)

    # -- network --------------------------------------------------------------------------
    async def _network_loop(self) -> None:
        while True:
            start = time.monotonic()
            online, latency = False, None
            try:
                _, w = await asyncio.wait_for(asyncio.open_connection("api.anthropic.com", 443), timeout=4)
                latency = round((time.monotonic() - start) * 1000)
                online = True
                w.close()
                with contextlib.suppress(Exception):
                    await w.wait_closed()
            except (OSError, asyncio.TimeoutError, socket.gaierror):
                pass
            quality = None if not online else ("Excellent" if latency < 80 else "Good" if latency < 200 else "Slow")
            self.network = {"online": online, "latency_ms": latency, "quality": quality}
            self.hub.emit("network", self.network)
            await asyncio.sleep(30)

    # -- weather --------------------------------------------------------------------------
    async def refresh_weather(self) -> None:
        city = self.f.settings["city"]
        units = self.f.settings["units"]
        if not city:
            self.weather = {"status": "unset"}
            self.hub.emit("weather", self.weather)
            return
        try:
            if not self._geo or self._geo[0] != city:
                g = await net.fetch_json("https://geocoding-api.open-meteo.com/v1/search?"
                                         + urlencode({"name": city, "count": 1}))
                hit = (g.get("results") or [None])[0]
                if not hit:
                    raise ValueError(f"couldn't find '{city}'")
                self._geo = (city, hit)
            hit = self._geo[1]
            w = await net.fetch_json("https://api.open-meteo.com/v1/forecast?" + urlencode({
                "latitude": hit["latitude"], "longitude": hit["longitude"], "timezone": "auto",
                "current": "temperature_2m,weather_code,wind_speed_10m,relative_humidity_2m,is_day",
                "daily": "temperature_2m_max,temperature_2m_min",
                "forecast_days": 1, "temperature_unit": units}))
            cur = w.get("current", {})
            daily = w.get("daily", {})
            self.weather = {
                "status": "ok", "place": ", ".join(x for x in (hit.get("name"), hit.get("country_code")) if x),
                "temp": round(cur.get("temperature_2m", 0)), "unit": "°F" if units == "fahrenheit" else "°C",
                "code": cur.get("weather_code"), "desc": WEATHER_CODES.get(cur.get("weather_code"), "—"),
                "humidity": cur.get("relative_humidity_2m"), "wind": cur.get("wind_speed_10m"),
                "is_day": cur.get("is_day", 1),
                "high": round((daily.get("temperature_2m_max") or [0])[0]),
                "low": round((daily.get("temperature_2m_min") or [0])[0]),
                "updated": time.time(),
            }
        except Exception as e:
            self.weather = {"status": "error", "error": str(e)[:160]}
        self.hub.emit("weather", self.weather)

    async def _weather_loop(self) -> None:
        while True:
            await self.refresh_weather()
            await asyncio.sleep(15 * 60)

    # -- calendar / timeline ----------------------------------------------------------------
    def timeline(self) -> dict:
        items = list(self.f.scheduler.today()) + [e for e in self.calendar["events"]]
        items.sort(key=lambda x: x["t"])
        return {"items": items, "calendar": self.calendar["status"], "calendar_error": self.calendar.get("error", "")}

    def request_calendar(self, force: bool = False) -> None:
        """Load today's Apple Calendar events in the background (cached for 5 minutes)."""
        fresh = time.time() - self.calendar["updated"] < 300
        if (fresh and not force) or (self._cal_task and not self._cal_task.done()):
            return
        self._cal_task = asyncio.create_task(self._load_calendar())

    async def _load_calendar(self) -> None:
        self.calendar["status"] = "loading"
        self.hub.emit("timeline", self.timeline())
        try:
            raw = await hands.osa(TODAY_EVENTS_SCRIPT, timeout=60)
            events = []
            for line in raw.splitlines():
                parts = line.split(hands.SEP)
                if len(parts) >= 3:
                    try:
                        t = datetime.fromisoformat(parts[0].strip()).timestamp()
                    except ValueError:
                        continue
                    events.append({"t": t, "title": parts[1].strip()[:120], "kind": "calendar",
                                   "calendar": parts[2].strip()[:60]})
            self.calendar = {"status": "ok", "events": events, "updated": time.time()}
        except Exception as e:
            msg = str(e)
            if "not allowed" in msg.lower() or "-1743" in msg:
                msg = "Allow Calendar access in System Settings → Privacy & Security → Automation."
            self.calendar = {"status": "error", "events": [], "updated": time.time(), "error": msg[:200]}
        self.hub.emit("timeline", self.timeline())
