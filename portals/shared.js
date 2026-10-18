/* Shared helpers for every Payer Lifecycle Console page. */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const sleep = ms => new Promise(r => setTimeout(r, ms));
const ROLE_LABEL = { benefits_analyst: "Benefits analyst", claims_analyst: "Claims analyst", manager: "Operations manager" };

async function api(path, opts = {}) {
  const r = await fetch("/api" + path, { headers: { "Content-Type": "application/json" }, credentials: "same-origin", ...opts,
    body: opts.body && typeof opts.body !== "string" ? JSON.stringify(opts.body) : opts.body });
  const data = await r.json().catch(() => ({}));
  if (r.status === 401 && !path.startsWith("/auth/login")) { location.href = "/"; throw new Error("Please sign in."); }
  if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail || r.statusText));
  return data;
}
const post = (path, body = {}) => api(path, { method: "POST", body });

let toastTimer;
function toast(msg, isErr = false) {
  let t = $("#toast");
  if (!t) { t = document.createElement("div"); t.id = "toast"; t.setAttribute("role", "status"); document.body.appendChild(t); }
  t.textContent = msg; t.style.background = isErr ? "var(--rose)" : ""; t.style.display = "block";
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.style.display = "none"), 4200);
}
async function guard(fn) { try { return await fn(); } catch (e) { toast(e.message, true); } }

/* ---------- market (US / India) ---------- */
let MARKET = "us";
try { MARKET = new URLSearchParams(location.search).get("m") || localStorage.getItem("payer_market") || "us"; } catch (_) { /* storage unavailable */ }
if (!["us", "in"].includes(MARKET)) MARKET = "us";
function setMarket(m) { MARKET = m; try { localStorage.setItem("payer_market", m); } catch (_) { /* ignore */ } }

/* ---------- icons and chrome ---------- */
const P = {
  doc: '<path d="M7 3h7l4 4v14H7z"/><path d="M14 3v4h4M10 12h5M10 16h5"/>', shield: '<path d="M12 3l7 3v5c0 5-3 8-7 10-4-2-7-5-7-10V6z"/><path d="M9 12l2 2 4-4"/>',
  layers: '<path d="M12 3l9 5-9 5-9-5z"/><path d="M3 13l9 5 9-5"/>', users: '<circle cx="9" cy="8" r="3"/><path d="M3 20c0-3 3-5 6-5s6 2 6 5M16 5a3 3 0 010 6M18 15c2 .6 3 2 3 5"/>',
  receipt: '<path d="M6 3h12v18l-3-2-3 2-3-2-3 2z"/><path d="M9 8h6M9 12h6"/>', inbox: '<path d="M3 13l3-8h12l3 8v6H3z"/><path d="M3 13h5l1 3h6l1-3h5"/>',
  scale: '<path d="M12 4v16M6 20h12M5 7h14"/><path d="M5 7l-3 7a3 3 0 006 0zM19 7l-3 7a3 3 0 006 0z"/>', check: '<path d="M5 12l5 5 9-10"/>', x: '<path d="M6 6l12 12M18 6L6 18"/>',
  arrow: '<path d="M5 12h14M13 6l6 6-6 6"/>', down: '<path d="M12 5v14M6 13l6 6 6-6"/>', spark: '<path d="M12 3l2.2 6.2L21 12l-6.8 2.8L12 21l-2.2-6.2L3 12l6.8-2.8z"/>',
  lock: '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 018 0v3"/>', play: '<path d="M7 4l13 8-13 8z"/>', download: '<path d="M12 4v11M7 11l5 5 5-5M5 20h14"/>', upload: '<path d="M12 16V5M7 9l5-5 5 5M5 20h14"/>'
};
const ico = n => `<svg class="i" viewBox="0 0 24 24" aria-hidden="true">${P[n]}</svg>`;
const MARK = `<svg viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="9" fill="#1D5BF0"/><g stroke="#fff" stroke-width="2.2" stroke-linecap="round" fill="none"><circle cx="9" cy="10" r="2.6" fill="#fff"/><circle cx="23" cy="10" r="2.6" fill="#fff"/><circle cx="16" cy="22" r="2.6" fill="#fff"/><path d="M11 11l3.5 8M21 11l-3.5 8M11.5 10h9"/></g></svg>`;
const chip = (t, c = "mute") => `<span class="chip ${c}">${esc(t)}</span>`;

let ME = null;
async function boot(allowed) {
  ME = await api("/auth/me");
  if (allowed && !allowed.includes(ME.role)) {
    document.body.innerHTML = `<main class="wrap" style="max-width:560px;padding-block:70px"><div class="panel2"><h1 style="font-size:26px">This page is not for your role</h1><p>You are signed in as <b>${esc(ME.name)}</b> (${esc(ROLE_LABEL[ME.role])}).</p><div class="row"><a class="btn primary" href="/console/">Go to the console</a><button class="btn" id="so">Sign out</button></div></div></main>`;
    $("#so").onclick = signOut; throw new Error("wrong role");
  }
  return ME;
}
async function signOut() { try { await post("/auth/logout"); } finally { location.href = "/"; } }

function topbar(crumbs = [], onMarket) {
  const html = `<header class="bar"><div class="wrap"><a class="logo" href="/console/">${MARK} Payer Lifecycle Console</a>
    <nav class="crumbs" aria-label="Breadcrumb">${crumbs.map(c => c.href ? `<a href="${c.href}">${esc(c.t)}</a>` : esc(c.t)).join("  /  ")}</nav>
    <div class="right row"><div class="seg" role="group" aria-label="Market">${["us", "in"].map(k => `<button data-market="${k}" aria-pressed="${MARKET === k}">${k === "us" ? "US" : "India"}</button>`).join("")}</div>
    <div class="user"><span class="avatar">${esc(ME.name[0])}</span><span>${esc(ME.name)}<br><span style="color:#B5C6E6">${esc(ROLE_LABEL[ME.role])}</span></span><button class="btn sm ghost" id="signout" style="margin-left:6px">Sign out</button></div></div></div></header>`;
  setTimeout(() => {
    $$("[data-market]").forEach(b => b.onclick = () => { if (b.dataset.market !== MARKET) { setMarket(b.dataset.market); onMarket && onMarket(); } });
    const so = $("#signout"); if (so) so.onclick = signOut;
  }, 0);
  return html;
}
