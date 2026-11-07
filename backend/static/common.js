// Shared helpers for all ResQNet pages.
const API = "";  // same origin

async function api(path, opts = {}) {
  const o = { headers: {}, ...opts };
  if (o.body && typeof o.body !== "string" && !(o.body instanceof FormData)) {
    o.body = JSON.stringify(o.body);
    o.headers["Content-Type"] = "application/json";
  }
  const r = await fetch(API + path, o);
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.blob();
  if (!r.ok) throw new Error((data && data.error) || r.statusText);
  return data;
}

function toast(msg) {
  let t = document.getElementById("toast");
  if (!t) { t = document.createElement("div"); t.id = "toast"; t.className = "toast"; document.body.appendChild(t); }
  t.textContent = msg; t.classList.add("show");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), 3200);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function header(active) {
  const links = [["/", "Rescue ops"], ["/alerts", "Early warning"], ["/mesh", "Mesh simulator"], ["/enroll", "Enrol"], ["/relief", "Relief & payouts"]];
  const el = document.createElement("header");
  el.className = "top";
  el.innerHTML = `<div class="brand">Res<span>Q</span>Net</div>
    <nav>${links.map(([h, t]) => `<a href="${h}" class="${h === active ? "on" : ""}">${t}</a>`).join("")}</nav>
    <div class="spacer"></div><div id="hdr-extra" class="stats"></div>
    <span id="mode-pill" class="pill">…</span>`;
  document.body.prepend(el);
  api("/api/health").then((h) => {
    const p = document.getElementById("mode-pill");
    p.textContent = `AI: ${h.ai} · chain: ${h.chain}`;
    p.className = "pill " + (h.ai === "gemini" ? "ok" : "warn");
  }).catch(() => {});
}

function liveUpdates(onUpdate) {
  let es;
  const connect = () => {
    es = new EventSource("/api/events");
    es.addEventListener("update", (e) => { try { onUpdate(JSON.parse(e.data)); } catch (_) {} });
    es.onerror = () => { es.close(); setTimeout(connect, 3000); };
  };
  connect();
}

function debounce(fn, ms) { let h; return (...a) => { clearTimeout(h); h = setTimeout(() => fn(...a), ms); }; }

function scoreClass(c) {
  if (c.status === "claimed") return "s-claimed";
  if (c.status === "closed") return "s-closed";
  return c.score >= 100 ? "s-crit" : c.score >= 75 ? "s-high" : c.score >= 45 ? "s-med" : "s-low";
}
function scoreColor(c) {
  return { "s-crit": "#f85149", "s-high": "#f0883e", "s-med": "#d29922", "s-low": "#3fb950", "s-claimed": "#a371f7", "s-closed": "#6e7681" }[scoreClass(c)];
}
function ago(t) {
  if (!t) return "";
  const m = Math.round((Date.now() / 1000 - t) / 60);
  return m < 60 ? `${m} min ago` : `${(m / 60).toFixed(1)} h ago`;
}
function txLink(tx, base) {
  if (!tx) return "";
  const short = tx.slice(0, 10) + "…";
  return base ? `<a class="mono" target="_blank" href="${base}${tx}">${short}</a>` : `<span class="mono" title="${tx}">${short} (sim)</span>`;
}
