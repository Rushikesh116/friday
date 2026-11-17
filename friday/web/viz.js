// Canvas/SVG visuals: the core orb, voice waveforms, system rings and the stock chart.

const TAU = Math.PI * 2;

function fitCanvas(canvas) {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w, h };
}

const MODE_COLORS = {
  idle: [53, 228, 255], listening: [49, 242, 165], thinking: [169, 139, 255],
  speaking: [72, 214, 255], off: [80, 105, 130], starting: [53, 228, 255],
};

// A rotating particle sphere with orbit rings; reacts to state and audio levels.
export class Orb {
  constructor(canvas) {
    this.canvas = canvas;
    this.mode = "starting";
    this.level = 0;          // smoothed audio level 0..1 (mic when listening, voice when speaking)
    this.target = 0;
    this.color = [...MODE_COLORS.starting];
    this.t = 0;
    const n = 560, golden = Math.PI * (3 - Math.sqrt(5));
    this.pts = Array.from({ length: n }, (_, i) => {
      const y = 1 - (i / (n - 1)) * 2, r = Math.sqrt(1 - y * y), th = golden * i;
      return [Math.cos(th) * r, y, Math.sin(th) * r];
    });
    // Pairs of nearby points for the faint mesh lines.
    this.links = [];
    for (let i = 0; i < n; i += 3) {
      for (let j = i + 1; j < n; j += 2) {
        const a = this.pts[i], b = this.pts[j];
        const d = (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2;
        if (d < 0.028) this.links.push([i, j]);
      }
    }
    this.frame = this.frame.bind(this);
    requestAnimationFrame(this.frame);
  }

  setMode(mode) { this.mode = MODE_COLORS[mode] ? mode : "idle"; }
  setLevel(v) { this.target = Math.max(0, Math.min(1, v)); }

  frame(ts) {
    requestAnimationFrame(this.frame);
    if (document.hidden || !this.canvas.isConnected || this.canvas.offsetParent === null) return;
    const dt = Math.min(0.05, (ts - (this.last || ts)) / 1000);
    this.last = ts;
    const speed = { thinking: 0.9, speaking: 0.45, listening: 0.35, off: 0.05 }[this.mode] ?? 0.18;
    this.t += dt * speed;
    this.level += (this.target - this.level) * 0.18;
    const want = MODE_COLORS[this.mode];
    for (let k = 0; k < 3; k++) this.color[k] += (want[k] - this.color[k]) * 0.06;
    this.draw();
  }

  draw() {
    const { ctx, w, h } = fitCanvas(this.canvas);
    ctx.clearRect(0, 0, w, h);
    const cx = w / 2, cy = h / 2;
    const base = Math.min(w, h) * 0.34;
    const pulse = this.mode === "off" ? 0 : this.level;
    const breathe = this.mode === "idle" ? Math.sin(this.t * 3) * 0.015 : 0;
    const R = base * (1 + pulse * 0.12 + breathe);
    const [r, g, b] = this.color.map(Math.round);
    const col = (a) => `rgba(${r},${g},${b},${a})`;

    // glow
    const glow = ctx.createRadialGradient(cx, cy, R * 0.1, cx, cy, R * 1.6);
    glow.addColorStop(0, col(0.22 + pulse * 0.25));
    glow.addColorStop(0.5, col(0.06));
    glow.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = glow;
    ctx.fillRect(0, 0, w, h);

    // orbit rings (flattened ellipses) behind the sphere
    ctx.lineWidth = 1;
    for (let k = 0; k < 3; k++) {
      ctx.save();
      ctx.translate(cx, cy);
      ctx.rotate(this.t * (0.3 + k * 0.17) * (k % 2 ? -1 : 1) + k);
      ctx.strokeStyle = col(0.18 - k * 0.04);
      ctx.setLineDash(k === 1 ? [3, 7] : []);
      ctx.beginPath();
      ctx.ellipse(0, 0, R * (1.32 + k * 0.17), R * (0.36 + k * 0.08), 0, 0, TAU);
      ctx.stroke();
      ctx.restore();
    }
    ctx.setLineDash([]);

    // rotate points
    const ay = this.t * 0.9, ax = 0.35 + Math.sin(this.t * 0.4) * 0.1;
    const cyA = Math.cos(ay), syA = Math.sin(ay), cxA = Math.cos(ax), sxA = Math.sin(ax);
    const proj = this.pts.map(([x, y, z]) => {
      let X = x * cyA + z * syA, Z = -x * syA + z * cyA;
      let Y = y * cxA - Z * sxA; Z = y * sxA + Z * cxA;
      const wob = this.mode === "speaking" ? 1 + Math.sin(x * 9 + this.t * 14) * pulse * 0.06 : 1;
      return [cx + X * R * wob, cy + Y * R * wob, Z];
    });

    // mesh
    ctx.lineWidth = 0.6;
    for (const [i, j] of this.links) {
      const a = proj[i], p = proj[j];
      const depth = (a[2] + p[2]) / 2;
      if (depth < -0.2) continue;
      ctx.strokeStyle = col(0.05 + (depth + 0.2) * 0.12);
      ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(p[0], p[1]); ctx.stroke();
    }
    // points
    for (const [x, y, z] of proj) {
      const a = 0.15 + (z + 1) * 0.38;
      const s = 0.6 + (z + 1) * 0.75;
      ctx.fillStyle = col(a);
      ctx.fillRect(x - s / 2, y - s / 2, s, s);
    }

    // inner core
    const core = ctx.createRadialGradient(cx, cy, 0, cx, cy, R * 0.55);
    core.addColorStop(0, `rgba(235,253,255,${0.28 + pulse * 0.4})`);
    core.addColorStop(0.35, col(0.16 + pulse * 0.2));
    core.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = core;
    ctx.beginPath(); ctx.arc(cx, cy, R * 0.55, 0, TAU); ctx.fill();

    // outer HUD ring with ticks + rotating arcs
    ctx.save();
    ctx.translate(cx, cy);
    ctx.strokeStyle = col(0.35);
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.arc(0, 0, R * 1.22, 0, TAU); ctx.stroke();
    for (let k = 0; k < 72; k++) {
      const ang = (k / 72) * TAU, long = k % 6 === 0;
      const r1 = R * 1.22, r2 = r1 + (long ? 8 : 4);
      ctx.strokeStyle = col(long ? 0.45 : 0.2);
      ctx.beginPath(); ctx.moveTo(Math.cos(ang) * r1, Math.sin(ang) * r1); ctx.lineTo(Math.cos(ang) * r2, Math.sin(ang) * r2); ctx.stroke();
    }
    ctx.lineWidth = 2.5;
    ctx.strokeStyle = col(0.75);
    const spin = this.t * (this.mode === "thinking" ? 2.4 : 1);
    for (let k = 0; k < 3; k++) {
      const st = spin + (k * TAU) / 3;
      ctx.beginPath(); ctx.arc(0, 0, R * 1.3, st, st + 0.5 + pulse * 0.6); ctx.stroke();
    }
    ctx.restore();

    // reflection floor
    const fy = cy + R * 1.55;
    const floor = ctx.createRadialGradient(cx, fy, 0, cx, fy, R * 1.4);
    floor.addColorStop(0, col(0.16));
    floor.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = floor;
    ctx.save(); ctx.scale(1, 0.22); ctx.beginPath(); ctx.arc(cx, fy / 0.22, R * 1.4, 0, TAU); ctx.fill(); ctx.restore();
  }
}

// Scrolling level bars (voice card + talk bar).
export class Wave {
  constructor(canvas, { bars = 48, mirror = false, color = "53,228,255" } = {}) {
    this.canvas = canvas; this.bars = bars; this.mirror = mirror; this.color = color;
    this.hist = new Array(bars).fill(0);
  }
  push(v) { this.hist.push(v); if (this.hist.length > this.bars) this.hist.shift(); }
  draw(active = true) {
    if (!this.canvas.isConnected || this.canvas.offsetParent === null) return;
    const { ctx, w, h } = fitCanvas(this.canvas);
    ctx.clearRect(0, 0, w, h);
    const n = this.hist.length, gap = 2, bw = Math.max(1.5, (w - gap * (n - 1)) / n);
    for (let i = 0; i < n; i++) {
      const v = this.mirror ? this.hist[n - 1 - i] : this.hist[i];
      const idleWobble = active ? 0.06 + 0.04 * Math.sin(performance.now() / 300 + i * 0.6) : 0.03;
      const bh = Math.max(2, (Math.max(v, idleWobble)) * h);
      const fade = this.mirror ? (n - i) / n : (i + 1) / n;
      ctx.fillStyle = `rgba(${this.color},${(active ? 0.25 : 0.12) + fade * (active ? 0.6 : 0.2)})`;
      const x = i * (bw + gap);
      ctx.fillRect(x, (h - bh) / 2, bw, bh);
    }
  }
}

export function ring(pct, label, sub, color = "#35e4ff") {
  const r = 42, c = 2 * Math.PI * r, v = Math.max(0, Math.min(100, pct || 0));
  const warn = v >= 90 ? "#ff5d7c" : v >= 75 ? "#ffb648" : color;
  return `<div class="ring"><svg viewBox="0 0 104 104">
    <circle cx="52" cy="52" r="${r}" fill="none" stroke="rgba(70,190,255,0.12)" stroke-width="7"/>
    <circle cx="52" cy="52" r="${r}" fill="none" stroke="${warn}" stroke-width="7" stroke-linecap="round"
      stroke-dasharray="${(c * v) / 100} ${c}" style="filter: drop-shadow(0 0 5px ${warn}); transition: stroke-dasharray .6s"/>
    <circle cx="52" cy="52" r="33" fill="none" stroke="rgba(70,190,255,0.18)" stroke-dasharray="2 4"/>
  </svg><div class="rv"><span>${label}</span><b>${Math.round(v)}%</b><small>${sub || ""}</small></div></div>`;
}

// Interactive SVG line chart for a price series: [[unix, close], ...].
export function stockChart(el, data) {
  const pts = data.points || [];
  if (pts.length < 2) { el.innerHTML = '<div class="empty">No price data.</div>'; return; }
  const W = 600, H = 150, pad = { l: 6, r: 6, t: 10, b: 18 };
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const lo = Math.min(...ys), hi = Math.max(...ys), span = hi - lo || 1;
  const X = (x) => pad.l + ((x - x0) / (x1 - x0 || 1)) * (W - pad.l - pad.r);
  const Y = (y) => pad.t + (1 - (y - lo) / span) * (H - pad.t - pad.b);
  const up = ys[ys.length - 1] >= ys[0];
  const col = up ? "#31f2a5" : "#ff5d7c";
  const line = pts.map((p, i) => `${i ? "L" : "M"}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join("");
  const area = `${line}L${X(x1).toFixed(1)},${H - pad.b}L${X(x0).toFixed(1)},${H - pad.b}Z`;
  const gid = `g${Math.random().toString(36).slice(2, 8)}`;
  const fmtD = (t) => new Date(t * 1000).toLocaleDateString([], { month: "short", day: "numeric" });
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
      <defs><linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stop-color="${col}" stop-opacity="0.35"/><stop offset="1" stop-color="${col}" stop-opacity="0"/></linearGradient></defs>
      <line x1="0" x2="${W}" y1="${Y(ys[0])}" y2="${Y(ys[0])}" stroke="rgba(170,200,230,.25)" stroke-dasharray="3 4"/>
      <path d="${area}" fill="url(#${gid})"/>
      <path d="${line}" fill="none" stroke="${col}" stroke-width="2" vector-effect="non-scaling-stroke"/>
      <line class="cross" x1="0" x2="0" y1="${pad.t}" y2="${H - pad.b}" stroke="rgba(220,240,255,.5)" visibility="hidden" vector-effect="non-scaling-stroke"/>
      <text x="${pad.l}" y="${H - 4}" fill="#7391ae" font-size="10">${fmtD(x0)}</text>
      <text x="${W - pad.r}" y="${H - 4}" fill="#7391ae" font-size="10" text-anchor="end">${fmtD(x1)}</text>
    </svg><div class="tip" hidden></div>`;
  const svg = el.querySelector("svg"), cross = el.querySelector(".cross"), tip = el.querySelector(".tip");
  svg.addEventListener("mousemove", (e) => {
    const rect = svg.getBoundingClientRect();
    const fx = ((e.clientX - rect.left) / rect.width) * W;
    let best = 0;
    for (let i = 1; i < pts.length; i++) if (Math.abs(X(pts[i][0]) - fx) < Math.abs(X(pts[best][0]) - fx)) best = i;
    const px = X(pts[best][0]);
    cross.setAttribute("x1", px); cross.setAttribute("x2", px); cross.setAttribute("visibility", "visible");
    tip.hidden = false;
    tip.style.left = `${(px / W) * 100}%`;
    tip.textContent = `${new Date(pts[best][0] * 1000).toLocaleString([], { month: "short", day: "numeric", hour: data.range === "5d" ? "numeric" : undefined })} · ${pts[best][1].toFixed(2)} ${data.currency || ""}`;
  });
  svg.addEventListener("mouseleave", () => { cross.setAttribute("visibility", "hidden"); tip.hidden = true; });
}
