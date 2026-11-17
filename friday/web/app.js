// Friday dashboard: live state over a WebSocket, rendered into the command-centre layout.
import { $, $$, esc, md, safeUrl, host, ago, until, clockTime, dayTime, stamp, dur, rate, money,
  icon, hydrateIcons, KIND_ICON, KIND_LABEL } from "./ui.js";
import { Orb, Wave, ring, stockChart } from "./viz.js";

const TOKEN = document.body.dataset.token;
const S = {
  connected: false, everConnected: false,
  state: {}, session: {}, runs: new Map(), chat: [], threads: [], feed: [], approvals: [], briefs: [],
  schedules: [], memory: [], settings: {}, voices: [], efforts: [], config: {},
  system: {}, weather: {}, network: {}, timeline: { items: [] }, level: { mic: 0, out: 0 },
  view: "overview", selRun: null, agentFilter: "active", brief: null, briefImg: 0, unreadChat: 0,
  approvalHidden: false,
};

// ========================= connection =========================
let ws = null, rid = 0, retry = 0;
const pending = new Map();

function connect() {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws?token=${encodeURIComponent(TOKEN)}`);
  ws.onopen = () => { retry = 0; S.connected = true; S.everConnected = true; dirty("status"); };
  ws.onmessage = (e) => { try { onMessage(JSON.parse(e.data)); } catch (err) { console.error(err); } };
  ws.onclose = () => {
    S.connected = false;
    dirty("status");
    for (const [, p] of pending) p({ error: "Connection lost" });
    pending.clear();
    setTimeout(connect, Math.min(8000, 400 * 2 ** retry++));
  };
}

// Send a command; resolves with the server's reply (never rejects; errors become {error}).
function send(type, data = {}, { quiet = false } = {}) {
  return new Promise((resolve) => {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      if (!quiet) toast("error", "Not connected to Friday", "Reconnecting…");
      return resolve({ error: "offline" });
    }
    const id = ++rid;
    const timer = setTimeout(() => { if (pending.delete(id)) resolve({ error: "No reply (timed out)" }); }, 60000);
    pending.set(id, (reply) => { clearTimeout(timer); resolve(reply || {}); });
    ws.send(JSON.stringify({ type, data, rid: id }));
  }).then((r) => {
    if (r && r.error && !quiet && r.error !== "offline") toast("error", "Couldn't do that", r.error);
    return r;
  });
}

// The sidebar controls (Assistant power, Always on / Push to talk, Voice replies) have their own
// commands, and Friday announces the new value as *state* to every open dashboard, while the
// controls and labels read it from settings. Show a change at once; if Friday refuses it, go
// back to the value Friday last reported.
const CONTROL_KEYS = ["power", "listen_mode", "voice_replies"];
async function setControl(key, value, type, data) {
  const before = S.settings[key];
  S.settings[key] = value;
  dirty("voice", "orb", "core", "bottom");
  const r = await send(type, data);
  if (r.error) {
    S.settings[key] = key in S.state ? S.state[key] : before;
    dirty("voice", "orb", "core", "bottom");
  }
  return r;
}

function onMessage({ type, data, rid: replyId }) {
  switch (type) {
    case "reply": { const p = pending.get(replyId); if (p) { pending.delete(replyId); p(data); } return; }
    case "snapshot": {
      Object.assign(S, {
        state: data.state || {}, session: data.session || {}, chat: data.chat || [], threads: data.threads || [], feed: data.feed || [],
        approvals: data.approvals || [], briefs: data.briefs || [], schedules: data.schedules || [],
        memory: data.memory || [], settings: data.settings || {}, voices: data.voices || [],
        efforts: data.efforts || [], config: data.config || {}, system: data.system || {},
        weather: data.weather || {}, network: data.network || {}, timeline: data.timeline || { items: [] },
      });
      S.runs = new Map((data.runs || []).map((r) => [r.id, r]));
      $("#brand-name").textContent = (S.config.name || "Friday").toUpperCase();
      $("#orb-name").textContent = (S.config.name || "Friday").toUpperCase();
      document.title = S.config.name || "Friday";
      renderAll();
      return;
    }
    case "state":
      Object.assign(S.state, data);
      for (const k of CONTROL_KEYS) if (k in data) S.settings[k] = data[k];  // see setControl
      dirty("status", "voice", "orb", "core", "bottom", "chatWorking", "model");
      if ("thread" in data) dirty("threads");
      if ("heard_at" in data) showHeard();
      return;
    case "session": S.session = data; dirty("model"); return;
    case "run": {
      const old = S.runs.get(data.id);
      if (data.steps === undefined) data.steps = old?.steps || [];
      const becameDone = old && old.status !== data.status && !["queued", "working", "waiting"].includes(data.status);
      S.runs.set(data.id, data);
      dirty("agentCards", "core", "agents", "researchActive", "chatWorking", "badges", "model");
      if (S.selRun === data.id) dirty(becameDone ? "runDetail" : "runHead");
      return;
    }
    case "step": {
      const run = S.runs.get(data.run);
      if (!run) return;
      (run.steps ||= []).push(data.step);
      if (run.steps.length > 400) run.steps.splice(0, run.steps.length - 400);
      if (S.selRun === data.run) appendStep(run, data.step);
      if (data.run === "conversation") dirty("chatWorking");
      return;
    }
    case "threads": S.threads = data || []; dirty("threads"); return;
    case "thread_chat":
      S.chat = data.chat || [];
      $("#chat-log").dataset.ready = "";
      dirty("chat", "threads");
      return;
    case "chat":
      if (data.thread && S.state.thread && data.thread !== S.state.thread) return;  // another chat: its list entry updates
      S.chat.push(data);
      if (S.chat.length > 300) S.chat.splice(0, S.chat.length - 300);
      if (S.view !== "chat" && (data.role === "friday" || data.role === "error")) S.unreadChat++;
      dirty("chat", "badges");
      return;
    case "feed":
      S.feed.unshift(data);
      S.feed.length = Math.min(S.feed.length, 80);
      dirty("feed");
      if (data.level !== "info" && !/^Research ready/.test(data.title)) {
        toast(data.level, data.title, data.detail, data.run_id ? () => openRun(data.run_id) : null);
      }
      return;
    case "approval":
      if (data.action === "new") { S.approvals.push(data); S.approvalHidden = false; }
      else S.approvals = S.approvals.filter((a) => a.id !== data.id);
      dirty("approvals", "badges");
      return;
    case "brief":
      S.briefs = [data, ...S.briefs.filter((b) => b.id !== data.id)];
      dirty("research", "latest", "badges");
      openBrief(data.id);
      return;
    case "schedules": S.schedules = data; dirty("schedules", "timeline", "badges", "core"); return;
    case "memory": S.memory = data; dirty("memory", "badges"); return;
    case "system": S.system = data; dirty("system"); return;
    case "network": S.network = data; dirty("bottom", "status"); return;
    case "weather": S.weather = data; dirty("bottom"); return;
    case "timeline": S.timeline = data; dirty("timeline"); return;
    case "level": S.level = data; return;
  }
}

// ========================= render scheduling =========================
const dirtySet = new Set();
let rafId = 0;
function dirty(...parts) { for (const p of parts) dirtySet.add(p); if (!rafId) rafId = requestAnimationFrame(flush); }
function flush() {
  rafId = 0;
  const parts = [...dirtySet];
  dirtySet.clear();
  for (const p of parts) { try { R[p]?.(); } catch (e) { console.error("render", p, e); } }
}
function renderAll() { dirty(...Object.keys(R)); }

const runs = () => [...S.runs.values()];
const isActive = (r) => ["queued", "working", "waiting"].includes(r.status);
const bgRuns = () => runs().filter((r) => r.kind !== "conversation");
const name = () => S.config.name || "Friday";

// ========================= renderers =========================
const R = {
  status() {
    const pill = $("#sys-pill"), txt = $("#sys-text");
    let cls = "ok", label = "Online";
    if (!S.connected) { cls = "bad"; label = S.everConnected ? "Reconnecting" : "Connecting"; }
    else if (S.network.online === false) { cls = "bad"; label = "Offline"; }
    else if (S.state.claude === "error") { cls = "warn"; label = "Degraded"; }
    else if (S.state.status === "starting") { cls = "warn"; label = "Starting"; }
    pill.className = `sys-pill ${cls}`;
    txt.textContent = label;
    const banner = $("#banner");
    if (S.connected && S.state.error) {
      banner.className = "banner";
      banner.innerHTML = `${icon("alert")}<span>${esc(S.state.error)}</span>`;
      banner.hidden = false;
    } else if (!S.connected && S.everConnected) {
      banner.className = "banner info";
      banner.innerHTML = `${icon("refresh")}<span>Lost connection to ${esc(name())}. Reconnecting… (If you closed the app window, start it again with <b>./friday.sh</b>.)</span>`;
      banner.hidden = false;
    } else banner.hidden = true;
  },

  voice() {
    const st = S.state, set = S.settings;
    const avail = st.voice_available !== false;
    const status = st.status || "starting";
    const labels = { idle: set.listen_mode === "ptt" ? "Push to talk" : "Listening", listening: "Hearing you…",
      thinking: "Working…", speaking: "Speaking", off: "Off", starting: "Starting…" };
    const vs = $("#voice-state");
    vs.textContent = !avail ? "Unavailable" : labels[status] || status;
    vs.className = "voice-state " + (avail ? ({ idle: "on", listening: "on", thinking: "busy", speaking: "speak" }[status] || "") : "");
    const btn = $("#mic-btn");
    btn.className = "mic-btn " + (!avail || status === "off" ? "off" : status === "listening" ? "listening"
      : status === "thinking" ? "busy" : status === "speaking" ? "speaking" : "");
    const hint = !avail ? "Voice is not available" : status === "off" ? "Tap to turn on"
      : status === "speaking" || status === "thinking" ? "Tap to stop" : status === "listening" ? "Listening…" : "Tap to speak";
    $("#mic-hint").textContent = hint;
    btn.setAttribute("aria-label", hint);
    $("#ctl-power").checked = !!set.power;
    $("#ctl-replies").checked = !!set.voice_replies;
    $("#mode-always").classList.toggle("on", set.listen_mode !== "ptt");
    $("#mode-ptt").classList.toggle("on", set.listen_mode === "ptt");
    for (const el of ["#ctl-power", "#ctl-replies"]) $(el).disabled = !avail;
    for (const b of $$(".seg button[data-mode]")) b.disabled = !avail;
  },

  orb() {
    const st = S.state.status || "starting";
    const map = { idle: "STANDBY", listening: "LISTENING", thinking: "THINKING", speaking: "SPEAKING", off: "OFFLINE", starting: "STARTING" };
    const el = $("#orb-status");
    el.textContent = map[st] || st.toUpperCase();
    el.className = `orb-status ${st}`;
    const conv = S.runs.get("conversation");
    let sub = "";
    if (st === "thinking" && conv?.current) sub = conv.current;
    else if (st === "idle") sub = S.settings.listen_mode === "ptt" ? "Press Talk to speak" : `Say “${name()}, …”`;
    else if (st === "off") sub = "Assistant is switched off";
    else if (st === "speaking") sub = `Say “${name()}, stop” to interrupt`;
    $("#orb-sub").textContent = sub.length > 70 ? sub.slice(0, 69) + "…" : sub;
    orb?.setMode(st);
  },

  core() {
    const st = S.state, set = S.settings;
    const active = bgRuns().filter(isActive);
    const subs = active.reduce((n, r) => n + (r.subs || []).filter((s) => s.status === "working").length, 0);
    const next = S.schedules.filter((s) => s.enabled !== false && s.next_run).sort((a, b) => a.next_run - b.next_run)[0];
    const items = [
      ["power", "Assistant", set.power ? "On" : "Off", set.power ? "ok" : "bad"],
      ["mic", "Listening", st.voice_available === false ? "Unavailable" : !set.power ? "Off"
        : set.listen_mode === "ptt" ? "Push to talk" : `Always · “${name()}”`, !set.power || st.voice_available === false ? "" : "ok"],
      ["speaker", "Voice", `${(set.voice || "—").replace(/ \(.*\)$/, "")} · replies ${set.voice_replies ? "on" : "off"}`, set.voice_replies ? "ok" : ""],
      ["agents", "Agents", active.length ? `${active.length} working${subs ? ` · ${subs} subagents` : ""}` : "Standby", active.length ? "busy" : ""],
      ["chip", (st.provider || set.provider || "claude") === "codex" ? "Codex" : "Claude", st.claude === "error" ? "Not connected" : `${shortModel(st.model || S.config.model)} · ${st.effort || set.effort || ""}`,
        st.claude === "error" ? "bad" : st.claude === "connected" ? "ok" : ""],
      ["clock", "Schedules", !set.schedules_enabled ? "Paused" : next ? `Next: ${next.name} ${until(next.next_run)}`
        : `${S.schedules.length} set`, set.schedules_enabled ? "ok" : "warn"],
    ];
    $("#core-list").innerHTML = items.map(([ic, t, v, cls]) =>
      `<li><span class="core-ico">${icon(ic)}</span><div><b>${esc(t)}</b><small class="${cls}">${esc(v)}</small></div></li>`).join("");
  },

  feed() {
    const el = $("#feed");
    if (!S.feed.length) { el.innerHTML = `<li class="empty" style="display:block"><b>Quiet so far</b>Finished tasks, approvals and research will show up here.</li>`; return; }
    const ic = { ok: "check", warn: "alert", error: "x", info: "info" };
    el.innerHTML = S.feed.slice(0, 40).map((f) => `<li class="${esc(f.level)}" ${f.run_id ? `data-run="${esc(f.run_id)}"` : ""}>
      <span class="fi">${icon(ic[f.level] || "info")}</span>
      <div><b>${esc(f.title)}</b>${f.detail ? `<small>${esc(f.detail)}</small>` : ""}</div>
      <time>${esc(ago(f.t))}</time></li>`).join("");
  },

  agentCards() {
    const list = runs().filter((r) => r.kind === "conversation" || isActive(r));
    const recent = bgRuns().filter((r) => !isActive(r)).sort((a, b) => (b.ended || 0) - (a.ended || 0));
    const show = [...list.sort((a, b) => (a.kind === "conversation" ? -1 : b.kind === "conversation" ? 1 : b.started - a.started)), ...recent].slice(0, 6);
    $("#agent-cards").innerHTML = show.map((r) => {
      const doing = isActive(r) || r.kind === "conversation" ? (r.current || (r.kind === "conversation" ? (S.state.status === "off" ? "Switched off" : "Ready") : "Starting…"))
        : (r.status === "done" ? "Finished" : r.error || r.status);
      const live = isActive(r) || (r.kind === "conversation" && ["thinking", "speaking"].includes(S.state.status));
      const status = r.kind === "conversation" ? (live ? "working" : "idle") : r.status;
      return `<div class="agent-card" data-run="${esc(r.id)}">
        <div class="top"><span class="kind-ico kind-${esc(r.kind)}">${icon(KIND_ICON[r.kind] || "task")}</span>
        <div style="min-width:0"><b>${esc(r.kind === "conversation" ? `${name()} (main)` : r.title)}</b><span class="st ${esc(status)}">${esc(statusLabel(status))}</span></div></div>
        <div class="doing">${esc(doing)}</div>
        <div class="meta"><span>${r.tool_calls || 0} steps</span>${(r.subs || []).length ? `<span>${r.subs.length} sub</span>` : ""}<span>${esc(KIND_LABEL[r.kind] || r.kind)}</span></div>
        ${live ? '<div class="bar"></div>' : ""}</div>`;
    }).join("") || `<div class="empty">No agents yet.</div>`;
  },

  timeline() {
    const items = (S.timeline.items || []).slice().sort((a, b) => a.t - b.t);
    const now = Date.now() / 1000;
    let html = items.length ? items.map((it) => `<li class="${esc(it.kind)} ${it.t < now ? "past" : ""}">
        <span class="tm">${esc(clockTime(it.t))}</span><span class="dotl"></span>
        <span class="tt" title="${esc(it.title)}">${esc(it.title)}</span>
        <span class="in">${it.t < now ? "done" : esc(until(it.t))}</span></li>`).join("")
      : `<li class="empty" style="display:block">Nothing else today.</li>`;
    $("#timeline").innerHTML = html;
    const note = S.timeline.calendar === "error" ? `Calendar: ${esc(S.timeline.calendar_error || "not available")}`
      : S.timeline.calendar === "loading" ? "Loading calendar…" : "";
    let noteEl = $("#timeline-note");
    if (!noteEl) { noteEl = document.createElement("div"); noteEl.id = "timeline-note"; noteEl.className = "timeline-note"; $("#timeline").after(noteEl); }
    noteEl.innerHTML = note;
  },

  system() {
    const s = S.system;
    if (!s || s.cpu === undefined) { $("#rings").innerHTML = '<div class="empty" style="grid-column:1/-1">Reading system…</div>'; return; }
    $("#rings").innerHTML = ring(s.cpu, "CPU", `${s.cores} cores`) +
      ring(s.ram, "RAM", `${s.ram_used_gb}/${s.ram_total_gb} GB`) +
      ring(s.disk, "DISK", `${s.disk_free_gb} GB free`);
    const bat = s.battery == null ? "" : `<span>${icon("battery")} <b>${s.battery}%</b>${s.plugged ? " ⚡" : ""}</span>`;
    $("#sys-foot").innerHTML = `${bat}<span>↑ <b>${esc(rate(s.net_up))}</b></span><span>↓ <b>${esc(rate(s.net_down))}</b></span><span>Up <b>${esc(dur(s.uptime_s).replace(/ \d+s$/, ""))}</b></span>`;
    for (const i of $$("#sys-foot i[data-icon]")) { i.style.width = "14px"; i.style.height = "14px"; i.style.verticalAlign = "-2px"; }
  },

  latest() {
    const b = S.briefs[0];
    const el = $("#latest-brief");
    if (!b) {
      el.innerHTML = `<div class="empty"><b>No research yet</b>Ask “${esc(name())}, what happened with …?” or use the Research tab.</div>`;
      return;
    }
    const img = b.images?.[0];
    el.innerHTML = `<div class="latest" data-brief="${esc(b.id)}">
      <div class="thumb" data-imgwrap>${img ? `<img data-src="${esc(img.url)}" alt="">` : ""}</div>
      <div><time>${esc(ago(b.created))}</time><b>${esc(b.title)}</b><p>${esc(b.summary)}</p></div></div>`;
    wireImages(el);
  },

  model() {
    const st = S.state, ses = S.session;
    const today = bgRuns().filter((r) => r.started > startOfDay()).length;
    const connection = st.claude === "error" ? ["Not connected", "bad"] : st.claude === "connected" ? ["Connected", "ok"] : ["Connecting", "warn"];
    $("#model-panel").innerHTML = `<div class="kv">
      <div><span>Model</span><b>${esc(shortModel(st.model || S.config.model))}</b></div>
      <div><span>Effort</span><b>${esc(st.effort || S.settings.effort || "—")}</b></div>
      <div><span>${esc((st.provider || S.settings.provider || "claude") === "codex" ? "Codex" : "Claude")}</span><b class="${connection[1]}">${connection[0]}</b></div>
      <div><span>Session cost</span><b>${esc(money(ses.cost_usd))}</b></div>
      <div><span>Turns</span><b>${ses.turns || 0}</b></div>
      <div><span>Agents today</span><b>${today}</b></div></div>`;
  },

  bottom() {
    const w = S.weather;
    const wIcon = (code, day) => code == null ? "cloud" : code === 0 || code === 1 ? (day ? "sun" : "moon")
      : code <= 3 ? "cloud" : code >= 95 ? "bolt" : (code >= 71 && code <= 77) || code === 85 || code === 86 ? "snow" : code >= 51 ? "rain" : "cloud";
    $("#chip-weather").innerHTML = w.status === "ok"
      ? `${icon(wIcon(w.code, w.is_day))}<div><span>${esc(w.place)}</span><b>${esc(w.temp)}${esc(w.unit)} ${esc(w.desc)}</b></div>`
      : w.status === "error" ? `${icon("cloud")}<div><span>Weather</span><b title="${esc(w.error)}">Unavailable</b></div>`
      : `${icon("cloud")}<div><span>Weather</span><a data-view-link="settings">Set your city</a></div>`;
    const n = S.network;
    $("#chip-network").innerHTML = n.online === false ? `${icon("wifioff")}<div><span>Network</span><b style="color:var(--red)">Offline</b></div>`
      : n.online ? `${icon("wifi")}<div><span>Network</span><b>${esc(n.quality)} · ${n.latency_ms} ms</b></div>`
      : `${icon("wifi")}<div><span>Network</span><b>Checking…</b></div>`;
    const st = S.state.status || "starting", set = S.settings;
    const avail = S.state.voice_available !== false;
    const pill = $("#talk-pill");
    pill.className = "talk-pill " + (!avail || st === "off" ? "off" : st === "listening" ? "listening" : st === "thinking" ? "thinking" : "");
    $("#talk-title").textContent = `TALK TO ${name().toUpperCase()}`;
    $("#talk-sub").textContent = !avail ? "Voice unavailable · use chat" : {
      idle: set.listen_mode === "ptt" ? "Click to speak" : `Listening for “${name()}” · click to speak`,
      listening: "I'm listening…", thinking: "Working on it · click to stop", speaking: `Speaking · say “${name()}, stop”`,
      off: "Assistant is off · click to turn on", starting: "Starting up…",
    }[st] || st;
  },

  badges() {
    const active = bgRuns().filter(isActive).length;
    setBadge("#badge-agents", active);
    setBadge("#badge-chat", S.view === "chat" ? 0 : S.unreadChat);
    setBadge("#badge-schedules", S.schedules.filter((s) => s.enabled !== false).length, "muted");
    setBadge("#badge-memory", S.memory.length, "muted");
    setBadge("#badge-research", bgRuns().filter((r) => r.kind === "research" && isActive(r)).length);
    setBadge("#badge-bell", S.approvals.length, "warn");
  },

  chat() {
    if (S.view !== "chat") return;
    const log = $("#chat-log");
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
    if (!S.chat.length) {
      log.innerHTML = `<div class="empty" style="margin:auto"><b>Say hello</b>Type below, or say “${esc(name())}, …” out loud. Spoken and typed messages both appear here.</div>`;
      return;
    }
    log.innerHTML = S.chat.map((m) => {
      if (m.role === "system") return `<div class="msg system">${esc(m.text)} · ${esc(clockTime(m.t))}</div>`;
      if (m.role === "error") return `<div class="msg error"><div class="bubble">${esc(m.text)}</div></div>`;
      const who = m.role === "user" ? "user" : "friday";
      const src = m.source === "voice" ? icon("mic") : m.source === "research" ? icon("globe") : icon("keyboard");
      const body = m.brief_id
        ? `${esc(m.text)}<div class="brief-chip"><button class="ghost sm" data-brief="${esc(m.brief_id)}">${icon("image")}Open brief</button></div>`
        : esc(m.text);
      return `<div class="msg ${who}"><div class="av">${who === "user" ? icon("user") : esc(name()[0])}</div>
        <div><div class="bubble">${body}</div><div class="when">${src}${esc(clockTime(m.t))}</div></div></div>`;
    }).join("");
    if (atBottom || !log.dataset.ready) log.scrollTop = log.scrollHeight;
    log.dataset.ready = "1";
  },

  threads() {
    const cur = S.threads.find((t) => t.id === S.state.thread);
    $("#chat-title").textContent = cur ? cur.title : "Conversation";
    if (S.view !== "chat") return;
    const list = $("#thread-list");
    if (!S.threads.length) { list.innerHTML = `<div class="empty">No chats yet</div>`; return; }
    list.innerHTML = S.threads.map((t) => `<div class="thread ${t.id === S.state.thread ? "active" : ""}" data-thread="${esc(t.id)}" role="button" tabindex="0">
        <div class="thread-top"><b>${esc(t.title)}</b><small>${esc(ago(t.updated))}</small></div>
        <div class="thread-bottom"><span>${esc(t.preview || "No messages yet")}</span>
          <button class="ghost sm icon-only" data-thread-del="${esc(t.id)}" title="Delete this chat">${icon("trash")}</button></div>
      </div>`).join("");
  },

  chatWorking() {
    const el = $("#chat-working");
    const conv = S.runs.get("conversation");
    const st = S.state.status;
    if (S.view !== "chat" || !conv || !["thinking", "speaking"].includes(st)) { el.hidden = true; return; }
    const lastTool = [...(conv.steps || [])].reverse().find((s) => s.kind === "tool" && !s.sub);
    const doing = st === "speaking" ? "Speaking…" : lastTool ? `${lastTool.tool}: ${lastTool.text}` : "Thinking…";
    el.innerHTML = `<span class="spinner"></span><span>${esc(doing)}</span><button class="ghost sm" data-action="stop">${icon("stop")}Stop</button>`;
    el.hidden = false;
  },

  agents() {
    if (S.view !== "agents") return;
    const all = bgRuns();
    const active = all.filter(isActive);
    const subsWorking = active.reduce((n, r) => n + (r.subs || []).filter((s) => s.status === "working").length, 0);
    $("#agent-stats").innerHTML = `
      <div><b>${active.filter((r) => r.status !== "queued").length}</b><span>Working</span></div>
      <div><b>${active.filter((r) => r.status === "queued").length}</b><span>Queued</span></div>
      <div><b>${subsWorking}</b><span>Subagents</span></div>
      <div><b>${all.filter((r) => r.status === "done" && r.ended > startOfDay()).length}</b><span>Done today</span></div>`;
    for (const b of $$("#agent-filter button")) b.classList.toggle("on", b.dataset.filter === S.agentFilter);
    const conv = S.runs.get("conversation");
    let list = S.agentFilter === "active" ? active : all;
    list = list.slice().sort((a, b) => (isActive(b) - isActive(a)) || ((b.ended || b.started) - (a.ended || a.started)));
    if (conv) list.unshift(conv);
    if (!S.selRun || !S.runs.has(S.selRun)) { S.selRun = list[1]?.id || list[0]?.id || null; dirty("runDetail"); }
    $("#run-list").innerHTML = list.map((r) => {
      const status = r.kind === "conversation" ? (["thinking", "speaking"].includes(S.state.status) ? "working" : "idle") : r.status;
      const sub = isActive(r) || r.kind === "conversation" ? (r.current || (r.kind === "conversation" ? "Voice & chat conversation" : "Starting…"))
        : (r.status === "done" ? firstLine(r.result) : r.error || r.status);
      const time = r.kind === "conversation" ? "" : isActive(r) ? dur(Date.now() / 1000 - r.started) : ago(r.ended);
      return `<button class="run-item ${r.id === S.selRun ? "sel" : ""}" data-run="${esc(r.id)}">
        <span class="kind-ico kind-${esc(r.kind)}">${icon(KIND_ICON[r.kind] || "task")}</span>
        <span style="min-width:0"><b>${esc(r.kind === "conversation" ? `${name()} · main conversation` : r.title)}</b><small>${esc(sub || "")}</small></span>
        <span class="rt"><span class="st ${esc(status)}">${esc(statusLabel(status))}</span><br>${esc(time)}${(r.subs || []).length ? ` · ${r.subs.length} sub` : ""}</span></button>`;
    }).join("") || `<div class="empty"><b>No active agents</b>Start a task above, or switch to “All”.</div>`;
  },

  runDetail() {
    if (S.view !== "agents") return;
    const el = $("#run-detail");
    const r = S.runs.get(S.selRun);
    if (!r) { el.innerHTML = `<div class="empty" style="margin:auto"><b>Pick an agent</b>Its live steps, subagents and result show here.</div>`; return; }
    if (!r.steps?.length && r.step_count && !r._fetched) {
      r._fetched = true;
      send("run_detail", { id: r.id }, { quiet: true }).then((d) => { if (d && d.id) { d._fetched = true; S.runs.set(d.id, d); dirty("runDetail"); } });
    }
    el.innerHTML = `<div class="rd-head" id="rd-head"></div>
      ${r.prompt && r.kind !== "conversation" ? `<div class="rd-prompt">${esc(r.prompt)}</div>` : ""}
      <div id="rd-subs"></div>
      <div class="rd-section">${icon("terminal")}Live steps</div>
      <ul class="steps" id="rd-steps"></ul>
      <div id="rd-result"></div>`;
    R.runHead();
    const steps = $("#rd-steps");
    steps.innerHTML = (r.steps || []).map(stepHTML).join("") || `<li class="empty" style="display:block">No steps yet.</li>`;
    steps.scrollTop = steps.scrollHeight;
  },

  runHead() {
    const r = S.runs.get(S.selRun);
    const head = $("#rd-head");
    if (!r || !head) return;
    const conv = r.kind === "conversation";
    const status = conv ? (["thinking", "speaking"].includes(S.state.status) ? "working" : "idle") : r.status;
    const elapsed = conv ? "" : isActive(r) ? `Running ${dur(Date.now() / 1000 - r.started)}` : r.ended ? `Took ${dur(r.ended - r.started)} · ${ago(r.ended)}` : "";
    head.innerHTML = `<span class="kind-ico kind-${esc(r.kind)}">${icon(KIND_ICON[r.kind] || "task")}</span>
      <div style="min-width:0"><h3>${esc(conv ? `${name()} · main conversation` : r.title)}</h3>
        <div class="sub"><span class="st ${esc(status)}">${esc(statusLabel(status))}</span><span>${esc(KIND_LABEL[r.kind] || r.kind)}</span>
        ${elapsed ? `<span data-elapsed>${esc(elapsed)}</span>` : ""}${r.meta?.model ? `<span>${esc(r.meta.route === "manual" ? "Manual" : `Auto: ${r.meta.route}`)} · ${esc(shortModel(r.meta.model))} · ${esc(r.meta.effort || "")}</span>` : ""}<span>${r.tool_calls || 0} tool calls</span>
        ${r.cost_usd ? `<span>${esc(money(r.cost_usd))}</span>` : ""}</div></div>
      <div class="acts">
        ${isActive(r) || (conv && status === "working") ? `<button class="danger sm" data-cancel="${esc(r.id)}">${icon("stop")}Stop</button>` : ""}
        ${!conv && !isActive(r) && ["task", "schedule", "briefing"].includes(r.kind) ? `<button class="ghost sm" data-rerun="${esc(r.id)}">${icon("refresh")}Run again</button>` : ""}
        ${r.kind === "research" && r.status === "done" ? `<button class="ghost sm" data-brief-run="${esc(r.id)}">${icon("image")}Open brief</button>` : ""}
      </div>`;
    const subsEl = $("#rd-subs");
    const subs = r.subs || [];
    subsEl.innerHTML = subs.length ? `<div class="rd-section">${icon("layers")}Subagents (${subs.length})</div><div class="subs">${subs.map((s) =>
      `<div class="sub-card"><b title="${esc(s.description)}">${esc(s.description)}</b>
        <small><span class="st ${esc(s.status)}">${esc(statusLabel(s.status))}</span> · ${esc(s.kind)}${s.last_tool ? ` · ${esc(s.last_tool)}` : ""}${s.tool_uses ? ` · ${s.tool_uses} tools` : ""}</small></div>`).join("")}</div>` : "";
    const res = $("#rd-result");
    if (res) {
      res.innerHTML = r.result && !isActive(r) && !conv ? `<div class="rd-section">${icon("check")}Result</div><div class="result md">${md(r.result)}</div>`
        : r.error && !isActive(r) ? `<div class="result err">${esc(r.error)}</div>` : "";
    }
  },

  researchActive() {
    if (S.view !== "research") return;
    const act = bgRuns().filter((r) => r.kind === "research" && isActive(r));
    $("#research-active").innerHTML = act.map((r) => `<div class="ra" data-run="${esc(r.id)}"><span class="spinner" style="width:14px;height:14px;border-radius:50%;border:2px solid rgba(255,209,102,.3);border-top-color:#ffd166;animation:spin .8s linear infinite"></span>
      <b>${esc(r.prompt)}</b><span class="cur">${esc(r.status === "queued" ? "Waiting for a free agent slot…" : r.current || "Starting…")}</span><span>${esc(dur(Date.now() / 1000 - r.started))}</span></div>`).join("");
  },

  research() {
    R.researchActive();
    if (S.view !== "research") return;
    const g = $("#brief-grid");
    if (!S.briefs.length) {
      g.innerHTML = `<article class="panel" style="grid-column:1/-1"><div class="empty"><b>No briefs yet</b>Ask about any topic above. The researcher reads the news, gathers photos and real figures, and builds an illustrated brief.</div></article>`;
      return;
    }
    g.innerHTML = S.briefs.map((b) => `<article class="panel brief-card" data-brief="${esc(b.id)}">
      <div class="img" data-imgwrap>${b.images?.[0] ? `<img data-src="${esc(b.images[0].url)}" alt="">` : ""}
        ${b.images?.length ? `<span class="count">${icon("image")}${b.images.length}</span>` : ""}</div>
      <div class="body"><b>${esc(b.title)}</b><p>${esc(b.summary)}</p><time>${esc(dayTime(b.created))}</time></div></article>`).join("");
    wireImages(g);
  },

  schedules() {
    $("#ctl-schedules").checked = !!S.settings.schedules_enabled;
    if (S.view !== "schedules") return;
    const el = $("#schedule-list");
    if (!S.schedules.length) {
      el.innerHTML = `<div class="empty"><b>No schedules yet</b>Create one here, or just say “${esc(name())}, every weekday at 8 summarize my email.”</div>`;
      return;
    }
    el.innerHTML = `<table class="sched-table"><thead><tr><th>Job</th><th>When</th><th>Next run</th><th>Last run</th><th>On</th><th></th></tr></thead><tbody>${
      S.schedules.map((s) => {
        const last = s.last_run ? `<span class="st ${esc(s.last_status || "")}">${esc(statusLabel(s.last_status || "done"))}</span> ${esc(ago(s.last_run))}` : '<span style="color:var(--dim)">Never</span>';
        return `<tr class="${s.enabled === false ? "off" : ""}">
          <td><b>${esc(s.name)}</b><small title="${esc(s.prompt)}">${esc(s.prompt)}</small></td>
          <td>${esc(s.when)}${s.speak ? ` <span title="Read aloud">${icon("speaker")}</span>` : ""}</td>
          <td>${s.next_run && s.enabled !== false ? `${esc(dayTime(s.next_run))}<small>${esc(until(s.next_run))}</small>` : '<span style="color:var(--dim)">—</span>'}</td>
          <td>${last}</td>
          <td><label class="switch-row"><input type="checkbox" data-sched-toggle="${esc(s.id)}" ${s.enabled !== false ? "checked" : ""}><span class="switch"></span></label></td>
          <td><button class="icon-sm" data-sched-run="${esc(s.id)}" title="Run now">${icon("play")}</button>
            <button class="icon-sm" data-sched-edit="${esc(s.id)}" title="Edit">${icon("edit")}</button>
            <button class="icon-sm del" data-sched-del="${esc(s.id)}" title="Delete">${icon("trash")}</button></td></tr>`;
      }).join("")}</tbody></table>`;
    for (const i of $$(".sched-table td i[data-icon='speaker']")) { i.style.width = "14px"; i.style.verticalAlign = "-2px"; i.style.color = "var(--cyan)"; }
  },

  memory() {
    if (S.view !== "memory") return;
    const el = $("#memory-list");
    el.innerHTML = S.memory.length ? S.memory.slice().reverse().map((m) => `<li><span class="core-ico">${icon("memory")}</span><p>${esc(m.text)}</p>
      <small>${m.source === "Friday" ? `noted by ${esc(name())}` : "added by you"} · ${esc(ago(m.created))}</small>
      <button class="icon-sm del" data-mem-del="${esc(m.id)}" title="Forget">${icon("trash")}</button></li>`).join("")
      : `<li class="empty" style="display:block"><b>Nothing saved yet</b>Notes you add here help ${esc(name())} personalise replies and emails.</li>`;
  },

  settings() {
    if (S.view !== "settings") return;
    const st = S.settings;
    const voiceOpts = S.voices.map((v) => `<option value="${esc(v.name)}" ${v.name === st.voice ? "selected" : ""}>${
      esc(v.name.includes("(") ? v.name : `${v.name} (${v.locale})`)}</option>`).join("");
    const effortOpts = S.efforts.map((e) => `<option value="${esc(e)}" ${e === st.effort ? "selected" : ""}>${esc(e)}</option>`).join("");
    const sw = (key, on) => `<label class="switch-row"><input type="checkbox" data-setting="${key}" ${on ? "checked" : ""}><span class="switch"></span></label>`;
    $("#settings").innerHTML = `
      <article class="panel"><h2>Voice</h2>
        <div class="set-row"><div><b>Voice</b><small>The voice ${esc(name())} speaks with. Download better ones in System Settings → Accessibility → Spoken Content.</small></div>
          <div class="ctl"><select data-setting="voice">${voiceOpts}</select><button class="icon-sm" data-action="preview-voice" title="Preview">${icon("play")}</button></div></div>
        <div class="set-row"><div><b>Speaking speed</b><small>Words per minute.</small></div>
          <div class="ctl"><input type="range" min="120" max="260" step="2" value="${st.rate}" data-setting="rate"><output>${st.rate}</output></div></div>
        <div class="set-row"><div><b>Volume</b><small>Relative to your Mac's volume.</small></div>
          <div class="ctl"><input type="range" min="0" max="100" step="5" value="${Math.round((st.volume ?? 0.75) * 100)}" data-setting="volume"><output>${Math.round((st.volume ?? 0.75) * 100)}%</output></div></div>
        <div class="set-row"><div><b>Interrupt by talking over her</b><small>Speaking while she talks makes her stop and listen. Turn off if a TV or other people keep cutting her off.</small></div><div class="ctl">${sw("barge_in", st.barge_in)}</div></div>
      </article>
      <article class="panel"><h2>Brain</h2>
        <div class="set-row"><div><b>Provider</b><small>Claude and Codex keep separate chat context. Mail, Calendar, browser actions, schedules and illustrated research use Claude even when Codex is selected.</small></div>
          <div class="ctl"><select data-setting="provider"><option value="claude" ${st.provider !== "codex" ? "selected" : ""}>Claude</option><option value="codex" ${st.provider === "codex" ? "selected" : ""}>Codex</option></select></div></div>
        <div class="set-row"><div><b>Model selection</b><small>Auto chooses a model and effort for each request. Manual uses the fixed model and effort below.</small></div>
          <div class="ctl"><select data-setting="model_mode"><option value="auto" ${st.model_mode !== "manual" ? "selected" : ""}>Automatic</option><option value="manual" ${st.model_mode === "manual" ? "selected" : ""}>Manual</option></select></div></div>
        <div class="set-row"><div><b>Claude manual model</b><small>Set with FRIDAY_MODEL; currently ${esc(shortModel(S.config.model))}. Auto uses Haiku, Sonnet or this model.</small></div><div class="ctl"><b>${esc(shortModel(S.config.model))}</b></div></div>
        <div class="set-row"><div><b>Codex model</b><small>Used for normal and hard Codex requests; auto uses Luna for quick ones. Codex works inside its workspace sandbox and does not show Friday approval dialogs for shell actions. Requires codex login.</small></div>
          <div class="ctl"><input type="text" maxlength="80" value="${esc(st.codex_model || "gpt-5.6-sol")}" data-setting="codex_model"></div></div>
        <div class="set-row"><div><b>Manual effort</b><small>Used when model selection is Manual. Auto uses low, medium or max by task.</small></div>
          <div class="ctl"><select data-setting="effort">${effortOpts}</select></div></div>
        <div class="set-row"><div><b>Agents at once</b><small>How many background agents may run in parallel. More finish sooner but use more of your plan.</small></div>
          <div class="ctl"><input type="range" min="1" max="6" step="1" value="${st.max_parallel}" data-setting="max_parallel"><output>${st.max_parallel}</output></div></div>
        <div class="set-row"><div><b>Fresh start</b><small>Clears the conversation so new settings and memory take effect.</small></div>
          <div class="ctl"><button class="ghost sm" data-action="new-conversation">${icon("refresh")}New conversation</button></div></div>
      </article>
      <article class="panel"><h2>Weather</h2>
        <div class="set-row"><div><b>City</b><small>Shown in the bottom bar. Leave empty to turn weather off (nothing is looked up).</small></div>
          <div class="ctl"><input type="text" maxlength="80" value="${esc(st.city || "")}" placeholder="e.g. Boston" data-setting="city"></div></div>
        <div class="set-row"><div><b>Units</b><small></small></div>
          <div class="ctl"><select data-setting="units"><option value="celsius" ${st.units === "celsius" ? "selected" : ""}>Celsius</option><option value="fahrenheit" ${st.units === "fahrenheit" ? "selected" : ""}>Fahrenheit</option></select></div></div>
      </article>
      <article class="panel"><h2>Automation</h2>
        <div class="set-row"><div><b>Run schedules</b><small>Pause every scheduled job at once without deleting them.</small></div><div class="ctl">${sw("schedules_enabled", st.schedules_enabled)}</div></div>
        <div class="set-row"><div><b>Name</b><small>Her name is also her wake word. Change it with FRIDAY_NAME and restart.</small></div><div class="ctl"><b>${esc(name())}</b></div></div>
      </article>`;
  },

  approvals() {
    const ov = $("#approval-overlay");
    const a = S.approvals[0];
    if (!a || S.approvalHidden) { ov.hidden = true; return; }
    $("#approval-agent").innerHTML = `From <b>${esc(a.run_id === "conversation" ? `${name()} (main conversation)` : a.agent)}</b> · ${esc(ago(a.created))}`;
    $("#approval-spoken").textContent = a.spoken;
    $("#approval-detail").textContent = a.detail;
    $("#approval-count").textContent = S.approvals.length > 1 ? `1 of ${S.approvals.length}` : "";
    $("#approval-hint").textContent = a.run_id === "conversation" && S.state.voice_available !== false && S.settings.power
      ? "You can also answer out loud: “yes” or “no”." : "";
    ov.hidden = false;
  },
};

// ========================= steps =========================
function stepHTML(s) {
  const ic = { tool: "terminal", say: "chat", sub: "layers", error: "alert", user: "user" }[s.kind] || "info";
  const label = s.kind === "tool" ? `<span class="tl">${esc(s.tool)}</span>` : s.kind === "user" ? '<span class="tl">You</span>' : "";
  return `<li class="${esc(s.kind)} ${s.sub ? "insub" : ""}"><time>${esc(stamp(s.t))}</time>${icon(ic)}<span>${label}${esc(s.text)}</span></li>`;
}

function appendStep(run, step) {
  const list = $("#rd-steps");
  if (!list || S.view !== "agents") return;
  const near = list.scrollHeight - list.scrollTop - list.clientHeight < 60;
  list.querySelector(".empty")?.remove();
  list.insertAdjacentHTML("beforeend", stepHTML(step));
  while (list.children.length > 400) list.firstElementChild.remove();
  if (near) list.scrollTop = list.scrollHeight;
}

// ========================= brief overlay =========================
let slideTimer = null;
function openBrief(id) {
  const b = S.briefs.find((x) => x.id === id);
  if (!b) return toast("warn", "That brief isn't available anymore");
  S.brief = id;
  S.briefImg = 0;
  renderBrief();
  $("#brief-overlay").hidden = false;
  $("#brief").scrollTop = 0;
  clearInterval(slideTimer);
  slideTimer = setInterval(() => {
    const cur = S.briefs.find((x) => x.id === S.brief);
    if (!cur || $("#brief-overlay").hidden || $("#brief .main-img:hover") || (cur.images || []).length < 2) return;
    showImage((S.briefImg + 1) % cur.images.length);
  }, 6500);
}

function closeBrief() { $("#brief-overlay").hidden = true; S.brief = null; clearInterval(slideTimer); }

function renderBrief() {
  const b = S.briefs.find((x) => x.id === S.brief);
  if (!b) return;
  const imgs = b.images || [];
  const gallery = imgs.length ? `<div class="gallery ${imgs.length === 1 ? "single" : ""}">
      <div class="main-img" data-imgwrap data-action="lightbox"></div>
      ${imgs.length > 1 ? `<div class="thumbs">${imgs.slice(0, 6).map((im, i) =>
        `<button data-img="${i}" class="${i === 0 ? "on" : ""}" data-imgwrap aria-label="Photo ${i + 1}"><img data-src="${esc(im.url)}" alt=""></button>`).join("")}</div>` : ""}
    </div>` : "";
  const figures = (b.figures || []).map((f) => `<div class="figure ${esc(f.trend)}"><span>${esc(f.label)}</span>
      <b>${f.trend === "down" ? icon("down") : f.trend === "up" ? icon("up") : ""}${esc(f.value)}</b>${f.detail ? `<small>${esc(f.detail)}</small>` : ""}</div>`).join("");
  $("#brief").innerHTML = `
    <div class="brief-head">
      <div><div class="tag">Research brief · ${esc(dayTime(b.created))}</div><h3 id="brief-title">${esc(b.title)}</h3>${b.subtitle ? `<p>${esc(b.subtitle)}</p>` : ""}</div>
      <div class="acts"><button class="ghost sm" data-action="brief-speak">${icon("speaker")}Read aloud</button>
        <button class="icon-btn" data-action="brief-close" aria-label="Close">${icon("x")}</button></div>
    </div>
    <div class="brief-body">
      ${gallery}
      <div class="summary-box">${esc(b.summary)}</div>
      <div class="story">${(b.sections || []).map((s) => `${s.heading ? `<h4>${esc(s.heading)}</h4>` : ""}${
        s.text.split(/\n{2,}/).map((p) => `<p>${esc(p)}</p>`).join("")}`).join("")}</div>
      <div class="side">
        ${figures ? `<section><h4>Key figures</h4><div class="figures">${figures}</div></section>` : ""}
        ${b.stock ? `<section><h4>Share price</h4><div class="stock" id="brief-stock"><div class="empty">Loading ${esc(b.stock.symbol)}…</div></div></section>` : ""}
        ${(b.timeline || []).length ? `<section><h4>Timeline</h4><ul class="tl-list">${b.timeline.map((t) => `<li><b>${esc(t.date)}</b>${esc(t.event)}</li>`).join("")}</ul></section>` : ""}
        ${b.caveats ? `<section><h4>Keep in mind</h4><div class="caveat">${esc(b.caveats)}</div></section>` : ""}
        ${(b.sources || []).length ? `<section><h4>Sources</h4><ul class="sources">${b.sources.map((s) => {
          const u = safeUrl(s.url);
          return u ? `<li><a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${icon("external")}<span>${esc(s.title)} <small>${esc(host(u))}</small></span></a></li>` : "";
        }).join("")}</ul></section>` : ""}
      </div>
    </div>`;
  for (const i of $$("#brief .sources i[data-icon], #brief .figure i[data-icon]")) { i.style.width = "14px"; i.style.height = "14px"; }
  showImage(0);
  wireImages($("#brief"));
  if (b.stock) loadStock(b.stock.symbol, b.stock.range);
}

function showImage(i) {
  const b = S.briefs.find((x) => x.id === S.brief);
  const imgs = b?.images || [];
  const main = $("#brief .main-img");
  if (!main || !imgs.length) return;
  // Skip pictures that failed to load.
  const failed = new Set($$("#brief .thumbs button").filter((x) => x.hidden).map((x) => +x.dataset.img));
  let idx = i, guard = 0;
  while (failed.has(idx) && guard++ < imgs.length) idx = (idx + 1) % imgs.length;
  S.briefImg = idx;
  const im = imgs[idx];
  main.innerHTML = `<img data-src="${esc(im.url)}" alt="${esc(im.caption)}"><div class="cap">${esc(im.caption)}${
    im.credit || im.source_url ? `<small>${esc(im.credit || "")}${im.credit && im.source_url ? " · " : ""}${im.source_url ? esc(host(im.source_url)) : ""}</small>` : ""}</div>`;
  wireImages(main);
  for (const t of $$("#brief .thumbs button")) t.classList.toggle("on", +t.dataset.img === idx);
}

async function loadStock(symbol, range) {
  const el = $("#brief-stock");
  if (!el) return;
  el.innerHTML = `<div class="empty">Loading ${esc(symbol)}…</div>`;
  let data;
  try {
    const r = await fetch(`/api/stock?symbol=${encodeURIComponent(symbol)}&range=${encodeURIComponent(range)}&token=${encodeURIComponent(TOKEN)}`);
    data = await r.json();
  } catch { data = { error: "network error" }; }
  if (!$("#brief-stock")) return;
  if (data.error) { el.innerHTML = `<div class="empty">Price data unavailable: ${esc(data.error)}</div>`; return; }
  const dir = data.change_pct >= 0 ? "up" : "down";
  el.innerHTML = `<div class="sh"><b>${esc(data.name)} (${esc(data.symbol)})</b><span class="${dir}">${data.change_pct >= 0 ? "+" : ""}${data.change_pct.toFixed(2)}%</span></div>
    <div style="font-size:12.5px;color:var(--muted)">${esc(data.last.toFixed(2))} ${esc(data.currency)} · range ${esc(data.low.toFixed(2))}–${esc(data.high.toFixed(2))}</div>
    <div class="chart" id="stock-chart"></div>
    <div class="ranges">${["5d", "1mo", "6mo", "1y", "5y"].map((rg) => `<button data-stock-range="${rg}" data-symbol="${esc(data.symbol)}" class="${rg === data.range ? "on" : ""}">${rg}</button>`).join("")}</div>`;
  stockChart($("#stock-chart"), data);
}

// Remote pictures: try direct, then via Friday's image fetcher, then hide.
function wireImages(root) {
  for (const img of root.querySelectorAll("img[data-src]")) {
    const url = safeUrl(img.dataset.src);
    img.removeAttribute("data-src");
    if (!url) { hideImg(img); continue; }
    img.referrerPolicy = "no-referrer";
    img.loading = "lazy";
    img.addEventListener("error", () => {
      if (!img.dataset.proxied) {
        img.dataset.proxied = "1";
        img.src = `/api/img?token=${encodeURIComponent(TOKEN)}&u=${encodeURIComponent(url)}`;
      } else hideImg(img);
    });
    img.src = url;
  }
}
function hideImg(img) {
  const wrap = img.closest("[data-imgwrap]");
  if (wrap && wrap.tagName === "BUTTON") wrap.hidden = true;
  img.remove();
}

// ========================= misc UI =========================
function setBadge(sel, n, extra = "") {
  const el = $(sel);
  el.hidden = !n;
  el.textContent = n > 99 ? "99+" : String(n);
  el.className = `badge ${extra}`.trim();
}

function statusLabel(s) {
  return { working: "Working", queued: "Queued", waiting: "Needs OK", idle: "Ready", done: "Done", failed: "Failed",
    cancelled: "Stopped", stopped: "Stopped", thinking: "Thinking", speaking: "Speaking" }[s] || s || "";
}
const shortModel = (m) => String(m || "").replace(/^claude-/, "").replace(/-(\d)-(\d)$/, " $1.$2").replace(/-(\d)$/, " $1").replace(/^./, (c) => c.toUpperCase());
const firstLine = (t) => String(t || "").replace(/[#*_`>]/g, "").split(/(?<=[.!?])\s|\n/)[0].slice(0, 160);
function startOfDay() { const d = new Date(); d.setHours(0, 0, 0, 0); return d.getTime() / 1000; }

let heardTimer = null;
function showHeard() {
  const t = S.state.heard, outcome = S.state.heard_outcome;
  if (!t) return;
  const label = { command: `for ${name()}`, answer: "your answer", stop: "stop", quiet: "quiet", ignored: "not for her", echo: "her own voice" }[outcome] || outcome;
  const h = $("#heard");
  h.innerHTML = `<em>Heard · ${esc(label)}</em>“${esc(t)}”`;
  h.hidden = false;
  const oh = $("#orb-heard");
  if (outcome !== "echo") { oh.textContent = `“${t}”`; oh.classList.add("show"); }
  clearTimeout(heardTimer);
  heardTimer = setTimeout(() => oh.classList.remove("show"), 6000);
}

function toast(level, title, detail = "", onClick = null) {
  const box = $("#toasts");
  const el = document.createElement("div");
  el.className = `toast ${level}`;
  const ic = { ok: "check", error: "alert", warn: "shield", info: "info" }[level] || "info";
  el.innerHTML = `${icon(ic)}<div><b>${esc(title)}</b>${detail ? `<small>${esc(String(detail).slice(0, 220))}</small>` : ""}</div>`;
  el.addEventListener("click", () => { onClick?.(); el.remove(); });
  box.prepend(el);
  while (box.children.length > 4) box.lastElementChild.remove();
  setTimeout(() => el.remove(), level === "error" ? 9000 : 6000);
}

function setView(v) {
  S.view = v;
  for (const b of $$("#nav button")) b.classList.toggle("active", b.dataset.view === v);
  for (const s of $$(".view")) s.classList.toggle("active", s.id === `view-${v}`);
  if (v === "chat") { S.unreadChat = 0; setTimeout(() => $("#chat-input").focus(), 30); }
  if (v === "overview") send("timeline", {}, { quiet: true }).then((t) => { if (t && t.items) { S.timeline = t; dirty("timeline"); } });
  const per = { chat: ["chat", "chatWorking", "threads"], agents: ["agents", "runDetail"], research: ["research"], schedules: ["schedules"],
    memory: ["memory"], settings: ["settings"], overview: ["orb", "agentCards", "system", "latest"] }[v] || [];
  dirty(...per, "badges");
}

function openRun(id) { S.selRun = id; setView("agents"); dirty("runDetail", "agents"); }

let promptHandler = null;
function openPrompt({ title, iconName = "sparkles", placeholder = "", submit = "Start", value = "" }, onSubmit) {
  $("#prompt-title").textContent = title;
  $("#prompt-icon").outerHTML = icon(iconName).replace("<i ", '<i id="prompt-icon" ');
  const input = $("#prompt-input");
  input.placeholder = placeholder;
  input.value = value;
  $("#prompt-submit").textContent = submit;
  promptHandler = onSubmit;
  $("#prompt-overlay").hidden = false;
  setTimeout(() => input.focus(), 30);
}

// ---- schedule editor ----
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
let editingSchedule = null;
function openScheduleEditor(s = null) {
  editingSchedule = s;
  $("#schedule-editor-title").textContent = s ? "Edit schedule" : "New schedule";
  $("#sch-name").value = s?.name || "";
  $("#sch-prompt").value = s?.prompt || "";
  $("#sch-kind").value = s?.kind || "daily";
  $("#sch-time").value = s?.time || "08:00";
  const mins = s?.every_minutes || 60;
  const hours = mins % 60 === 0;
  $("#sch-every").value = hours ? mins / 60 : mins;
  $("#sch-every-unit").value = hours ? "60" : "1";
  const d = new Date(Date.now() + 3600e3); d.setMinutes(0, 0, 0);
  const local = new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  $("#sch-at").value = s?.at || local;
  const days = new Set(s?.days || [0, 1, 2, 3, 4]);
  $("#sch-days").innerHTML = DAYS.map((n, i) => `<button type="button" data-day="${i}" class="${days.has(i) ? "on" : ""}">${n}</button>`).join("");
  $("#sch-speak").checked = !!s?.speak;
  $("#sch-error").hidden = true;
  syncScheduleKind();
  $("#schedule-overlay").hidden = false;
  setTimeout(() => $("#sch-name").focus(), 30);
}
function syncScheduleKind() {
  const k = $("#sch-kind").value;
  for (const el of $$("#schedule-form [data-for]")) {
    const on = el.dataset.for.split(" ").includes(k);
    el.hidden = !on;
    // Hidden fields are disabled so the browser doesn't validate (and block on) them.
    for (const input of el.querySelectorAll("input, select, button")) input.disabled = !on;
  }
}

// ========================= events =========================
document.addEventListener("click", async (e) => {
  const t = e.target.closest("[data-action],[data-view],[data-view-link],[data-run],[data-brief],[data-cancel],[data-rerun],[data-brief-run],[data-img],[data-stock-range],[data-sched-run],[data-sched-edit],[data-sched-del],[data-mem-del],[data-day],[data-thread],[data-thread-del]");
  if (!t) return;
  const d = t.dataset;
  if (d.view) return setView(d.view);
  if (d.viewLink) { e.preventDefault(); return setView(d.viewLink); }
  if (d.cancel) { const r = await send("cancel_run", { id: d.cancel }); if (r.ok) toast("info", "Stopping…"); return; }
  if (d.rerun) {
    const r = S.runs.get(d.rerun);
    if (r) { const res = await send("task", { prompt: r.prompt, title: r.title }); if (res.run) { toast("ok", "Started again", r.title); openRun(res.run); } }
    return;
  }
  if (d.briefRun) { const b = S.briefs.find((x) => x.run_id === d.briefRun); return b ? openBrief(b.id) : toast("warn", "No brief for this run"); }
  if (d.img !== undefined && t.closest("#brief")) return showImage(+d.img);
  if (d.stockRange) return loadStock(d.symbol, d.stockRange);
  if (d.schedRun) { const r = await send("schedule_run", { id: d.schedRun }); if (r.ok) toast("ok", "Running now"); return; }
  if (d.schedEdit) return openScheduleEditor(S.schedules.find((s) => s.id === d.schedEdit));
  if (d.schedDel) {
    const s = S.schedules.find((x) => x.id === d.schedDel);
    if (s && confirm(`Delete the schedule “${s.name}”?`)) await send("schedule_delete", { id: s.id });
    return;
  }
  if (d.threadDel) {
    const th = S.threads.find((x) => x.id === d.threadDel);
    if (th && confirm(`Delete the chat “${th.title}”? Its messages will be gone.`)) await send("thread_delete", { id: th.id });
    return;
  }
  if (d.thread) {
    $("#chat-layout").classList.remove("show-threads");
    if (d.thread !== S.state.thread) await send("thread_switch", { id: d.thread });
    return;
  }
  if (d.memDel) { await send("memory_delete", { id: d.memDel }); return; }
  if (d.day !== undefined) { t.classList.toggle("on"); return; }
  if (d.brief) return openBrief(d.brief);
  if (d.run && !d.action) return openRun(d.run);

  switch (d.action) {
    case "mic": case "talk-pill": {
      const st = S.state.status;
      if (S.state.voice_available === false) return toast("warn", "Voice isn't available", S.state.error || "Use the chat instead.");
      if (!S.settings.power || st === "off") return setControl("power", true, "power", { on: true });
      if (st === "speaking" || st === "thinking") return send("stop");
      return send("talk");
    }
    case "mode": return setControl("listen_mode", d.mode, "listen_mode", { mode: d.mode });
    case "stop": return send("stop");
    case "briefing": { const r = await send("briefing"); if (r.run) toast("ok", "Briefing started", "A background agent is gathering it."); return; }
    case "quick-task": return openPrompt({ title: "New background task", iconName: "task", placeholder: "Describe the job, e.g. “Find three cheap flights from Boston to Chicago next weekend and list them.”", submit: "Start agent" },
      async (text) => { const r = await send("task", { prompt: text }); if (r.run) { toast("ok", "Agent started"); openRun(r.run); } });
    case "quick-research": return openPrompt({ title: "Research a topic", iconName: "globe", placeholder: "e.g. “What happened to Nvidia's stock this month?”", submit: "Research" },
      async (text) => { const r = await send("research", { topic: text }); if (r.run) { toast("ok", "Researcher started", "The brief will pop up when it's ready."); setView("research"); } });
    case "quick-mail": send("chat", { text: "Check my email: summarize what's unread and anything that needs my attention." }); return setView("chat");
    case "new-conversation": {
      const r = await send("new_conversation");
      if (r.ok) { $("#chat-layout").classList.remove("show-threads"); setView("chat"); }
      return;
    }
    case "threads-toggle": $("#chat-layout").classList.toggle("show-threads"); dirty("threads"); return;
    case "thread-rename": {
      const cur = S.threads.find((t) => t.id === S.state.thread);
      if (!cur) return;
      return openPrompt({ title: "Rename chat", iconName: "edit", submit: "Rename", value: cur.title },
        (title) => send("thread_rename", { id: cur.id, title }));
    }
    case "open-approvals": if (!S.approvals.length) return toast("info", "Nothing needs your OK"); S.approvalHidden = false; return dirty("approvals");
    case "approve-yes": case "approve-no": {
      const a = S.approvals[0];
      if (!a) return;
      S.approvals.shift();
      dirty("approvals", "badges");
      return send("approve", { id: a.id, ok: d.action === "approve-yes" });
    }
    case "agent-filter": S.agentFilter = d.filter; return dirty("agents");
    case "chat-as-task": case "chat-as-research": {
      const input = $("#chat-input"), text = input.value.trim();
      if (!text) return toast("warn", "Type something first");
      const r = d.action === "chat-as-task" ? await send("task", { prompt: text }) : await send("research", { topic: text });
      if (r.run) { input.value = ""; toast("ok", d.action === "chat-as-task" ? "Agent started" : "Researcher started", text); }
      return;
    }
    case "brief-close": return closeBrief();
    case "brief-speak": { const b = S.briefs.find((x) => x.id === S.brief); if (b) send("speak", { text: b.summary }); return; }
    case "lightbox": {
      const b = S.briefs.find((x) => x.id === S.brief), im = b?.images?.[S.briefImg];
      const src = $("#brief .main-img img")?.src;
      if (!im || !src) return;
      const lb = $("#lightbox");
      lb.innerHTML = `<div><img src="${esc(src)}" alt="${esc(im.caption)}" referrerpolicy="no-referrer"><p>${esc(im.caption)}</p></div>`;
      lb.hidden = false;
      return;
    }
    case "schedule-new": return openScheduleEditor();
    case "schedule-close": $("#schedule-overlay").hidden = true; return;
    case "prompt-close": $("#prompt-overlay").hidden = true; return;
    case "preview-voice": return send("preview_voice", { voice: $("[data-setting='voice']").value });
  }
});

$("#lightbox").addEventListener("click", () => { $("#lightbox").hidden = true; });
$("#brief-overlay").addEventListener("click", (e) => { if (e.target.id === "brief-overlay") closeBrief(); });

document.addEventListener("change", (e) => {
  const t = e.target;
  if (t.id === "ctl-power") return setControl("power", t.checked, "power", { on: t.checked });
  if (t.id === "ctl-replies") return setControl("voice_replies", t.checked, "voice_replies", { on: t.checked });
  if (t.id === "ctl-schedules") return send("setting", { key: "schedules_enabled", value: t.checked });
  if (t.dataset.schedToggle) return send("schedule_toggle", { id: t.dataset.schedToggle, enabled: t.checked });
  if (t.id === "sch-kind") return syncScheduleKind();
  if (t.dataset.setting) {
    const key = t.dataset.setting;
    let value = t.type === "checkbox" ? t.checked : t.value;
    if (key === "volume") value = Number(value) / 100;
    if (key === "rate" || key === "max_parallel") value = Number(value);
    send("setting", { key, value }).then((r) => {
      if (r.settings) { S.settings = r.settings; if (["effort", "model_mode", "provider", "codex_model"].includes(key)) toast("info", "Brain setting updated", "Applies to the next request."); }
      else if (r.error) dirty("settings");
      dirty("voice", "core", "model", "orb", "schedules", "settings");
    });
  }
});

document.addEventListener("input", (e) => {
  const t = e.target;
  if (t.type === "range" && t.dataset.setting) {
    const out = t.parentElement.querySelector("output");
    if (out) out.textContent = t.dataset.setting === "volume" ? `${t.value}%` : t.value;
  }
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (!$("#lightbox").hidden) return ($("#lightbox").hidden = true);
    if (!$("#prompt-overlay").hidden) return ($("#prompt-overlay").hidden = true);
    if (!$("#schedule-overlay").hidden) return ($("#schedule-overlay").hidden = true);
    if (!$("#brief-overlay").hidden) return closeBrief();
    if (!$("#approval-overlay").hidden) { S.approvalHidden = true; return dirty("approvals"); }
  }
  if (!$("#brief-overlay").hidden && (e.key === "ArrowRight" || e.key === "ArrowLeft")) {
    const b = S.briefs.find((x) => x.id === S.brief);
    const n = b?.images?.length || 0;
    if (n > 1) showImage((S.briefImg + (e.key === "ArrowRight" ? 1 : n - 1)) % n);
  }
});

$("#ask-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const input = $("#ask-input"), text = input.value.trim();
  if (!text) return;
  send("chat", { text });
  input.value = "";
  setView("chat");
});

$("#chat-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const input = $("#chat-input"), text = input.value.trim();
  if (!text) return;
  send("chat", { text });
  input.value = "";
});
$("#chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#chat-form").requestSubmit(); }
});

$("#task-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const prompt = $("#task-prompt").value.trim();
  if (!prompt) return toast("warn", "Describe the task first");
  const r = await send("task", { prompt, title: $("#task-title").value.trim(), speak: $("#task-speak").checked });
  if (r.run) { $("#task-prompt").value = ""; $("#task-title").value = ""; S.selRun = r.run; S.agentFilter = "active"; dirty("agents", "runDetail"); toast("ok", "Agent started"); }
});

$("#research-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const topic = $("#research-input").value.trim();
  if (!topic) return;
  const r = await send("research", { topic, speak: $("#research-speak").checked });
  if (r.run) { $("#research-input").value = ""; toast("ok", "Researcher started", "The brief pops up when it's ready."); dirty("research"); }
});

$("#memory-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = $("#memory-input").value.trim();
  if (!text) return;
  const r = await send("memory_add", { text });
  if (r.ok) $("#memory-input").value = "";
});

$("#prompt-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = $("#prompt-input").value.trim();
  if (!text) return;
  $("#prompt-overlay").hidden = true;
  promptHandler?.(text);
});

$("#schedule-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const kind = $("#sch-kind").value;
  const payload = {
    name: $("#sch-name").value.trim(), prompt: $("#sch-prompt").value.trim(), kind,
    speak: $("#sch-speak").checked, enabled: editingSchedule ? editingSchedule.enabled !== false : true,
  };
  if (kind === "daily" || kind === "weekly") payload.time = $("#sch-time").value;
  if (kind === "weekly") payload.days = $$("#sch-days button.on").map((b) => +b.dataset.day);
  if (kind === "interval") payload.every_minutes = Math.round(Number($("#sch-every").value) * Number($("#sch-every-unit").value));
  if (kind === "once") payload.at = $("#sch-at").value;
  if (editingSchedule) payload.id = editingSchedule.id;
  const r = await send("schedule_save", payload, { quiet: true });
  if (r.error) { $("#sch-error").textContent = r.error; $("#sch-error").hidden = false; return; }
  $("#schedule-overlay").hidden = true;
  toast("ok", editingSchedule ? "Schedule updated" : "Schedule created", r.schedule?.when || "");
});

// City field: save on Enter/blur rather than every keystroke.
document.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target.dataset?.setting === "city") e.target.blur();
});

// ========================= clock & animation =========================
function tickClock() {
  const now = new Date();
  $("#clock-date").textContent = now.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long", year: "numeric" });
  const time = now.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const sec = String(now.getSeconds()).padStart(2, "0");
  $("#clock-time").innerHTML = `${esc(time)}<small>${sec}</small>`;
}

let orb = null;
const waves = {};
let lastWave = 0;
function animate(ts) {
  requestAnimationFrame(animate);
  if (document.hidden) return;
  const st = S.state.status;
  const lvl = st === "speaking" ? S.level.out : st === "off" ? 0 : S.level.mic;
  orb?.setLevel(lvl);
  if (ts - lastWave > 60) {
    lastWave = ts;
    const active = st !== "off" && S.state.voice_available !== false && S.connected;
    for (const w of Object.values(waves)) { w.push(active ? lvl : 0); w.draw(active); }
  }
}

function init() {
  hydrateIcons();
  orb = new Orb($("#orb"));
  waves.side = new Wave($("#side-wave"), { bars: 40 });
  waves.left = new Wave($("#talk-wave-l"), { bars: 30, mirror: true });
  waves.right = new Wave($("#talk-wave-r"), { bars: 30 });
  tickClock();
  setInterval(tickClock, 1000);
  // Elapsed times, "in 5m" labels and relative dates.
  setInterval(() => {
    const anyActive = runs().some(isActive);
    if (anyActive) dirty("agentCards", "agents", "researchActive", "runHead");
    dirty("feed", "timeline", "core");
  }, 1000 * 5);
  setInterval(() => { if (runs().some(isActive) && S.view === "agents") R.runHead(); }, 1000);
  requestAnimationFrame(animate);
  connect();
  window.FridayDebug = { state: S, dispatch: onMessage };  // for local troubleshooting only
}

init();
