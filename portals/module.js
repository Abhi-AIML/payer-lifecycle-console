/* Module shell shared by every use case: sidebar navigation, overview page, reference tables, system configuration.
   A page supplies a config object and its own agent views; this file owns everything around them. Routing is by URL hash. */
P.gear = '<circle cx="12" cy="12" r="3"/><path d="M19 12a7 7 0 00-.1-1.2l2-1.5-2-3.4-2.3.9a7 7 0 00-2-1.2L14.2 3h-4l-.4 2.6a7 7 0 00-2 1.2l-2.3-.9-2 3.4 2 1.5A7 7 0 005 12a7 7 0 00.1 1.2l-2 1.5 2 3.4 2.3-.9a7 7 0 002 1.2l.4 2.6h4l.4-2.6a7 7 0 002-1.2l2.3.9 2-3.4-2-1.5c.1-.4.1-.8.1-1.2z"/>';
P.table = '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 10h18M9 5v14"/>';
P.grid = '<rect x="4" y="4" width="7" height="7" rx="1.5"/><rect x="13" y="4" width="7" height="7" rx="1.5"/><rect x="4" y="13" width="7" height="7" rx="1.5"/><rect x="13" y="13" width="7" height="7" rx="1.5"/>';
const WORK_ROLE = { benefits: "benefits_analyst", claims: "claims_analyst", denials: "claims_analyst" };
const modView = () => (location.hash || "#overview").slice(1) || "overview";
const modMoney = n => { const c = META[MARKET].currency; return c.symbol + Number(n).toLocaleString(c.locale, { maximumFractionDigits: 2 }); };

function sideHtml(cfg, active) {
  return `<aside class="side" aria-label="Module navigation"><div class="side-head"><b>${esc(cfg.title)}</b><span>${esc(cfg.sub)}</span></div>
  ${cfg.groups().map(g => `<div class="side-lab">${esc(g.label)}</div>${g.items.map(it => `<a class="side-item ${it.id === active ? "active" : ""}" href="#${it.id}" ${it.id === active ? 'aria-current="page"' : ""}>
    <span class="side-ic ${it.done ? "done" : ""}">${it.done ? "✓" : it.num != null ? it.num : ico(it.icon || "doc")}</span><span class="side-tx"><b>${esc(it.name)}</b><i>${esc(it.sub || "")}</i></span></a>`).join("")}`).join("")}</aside>`;
}

function overviewHtml(o) {
  return `<section class="mhero"><div class="kicker">${esc(o.kicker)}</div><div class="row" style="gap:14px;margin:8px 0 6px"><h1>${esc(o.title)}</h1><span class="live-badge">Live</span></div>
  <p>${esc(o.desc)}</p><div class="mkpis">${o.kpis.map(([n, l]) => `<div><b>${esc(n)}</b><span>${esc(l)}</span></div>`).join("")}</div><br>
  <button class="btn light" id="launch">${ico("play")} Launch Demo ${ico("arrow")}</button></section>
  <section class="what"><div class="kicker" style="color:var(--muted);margin-bottom:6px">What the agents do</div>${o.steps.map((s, i) =>
    `<div class="what-step"><span class="what-n">${i + 1}</span><span><b>${esc(s[0])}:</b> ${esc(s[1])}</span></div>`).join("")}</section>`;
}

/* previous / next buttons at the foot of an agent page */
function agentNav(prev, next) {
  return `<div class="agentnav">${prev ? `<a class="btn" href="#${prev[0]}">← ${esc(prev[1])}</a>` : ""}${next ? `<a class="btn primary right" href="#${next[0]}">${esc(next[1])} →</a>` : ""}</div>`;
}

const Mod = {
  render(cfg) {
    let v = modView();
    const special = v.startsWith("ref-") || v === "settings";
    if (!cfg.views[v] && !special) v = "overview";
    $("#app").innerHTML = `${topbar(cfg.crumbs, cfg.onMarket)}<div class="mod">${sideHtml(cfg, v)}<main class="mod-main" id="modmain">
      ${cfg.status ? `<div class="statusrow">${cfg.status()}</div>` : ""}<div id="viewbody">${cfg.views[v] ? cfg.views[v]() : `<div class="panel2 muted">Loading…</div>`}</div></main></div>`;
    const ln = $("#launch"); if (ln) ln.onclick = cfg.launch;
    if (v.startsWith("ref-")) refView(cfg.uc, v.slice(4));
    if (v === "settings") settingsView(cfg.uc);
    return v;
  }
};

/* ---------------- reference tables ---------------- */
async function refView(uc, name) {
  const mount = $("#viewbody");
  let t; try { t = await api(`/reference/${uc}/${name}?market=${MARKET}`); } catch (e) { mount.innerHTML = `<div class="gate">${esc(e.message)}</div>`; return; }
  if (modView() !== "ref-" + name) return;
  const cell = (c, v) => c.key === "code" || c.key === "id" ? `<td style="white-space:nowrap"><span class="cite">${esc(v ?? "—")}</span></td>` : c.fmt === "money" ? `<td class="num">${v == null ? "—" : modMoney(v)}</td>` : c.fmt === "num" ? `<td class="num">${esc(v)}</td>` : `<td>${esc(v ?? "—")}</td>`;
  mount.innerHTML = `<div class="panel2"><div class="row"><h2>${esc(t.title)}</h2>${t.json === undefined ? chip(`${t.rows.length} rows`, "mute") : ""}</div><p class="muted" style="margin:6px 0 16px">${esc(t.note)}</p>
    ${t.json !== undefined ? (t.json ? `<pre class="code" style="max-height:560px">${esc(JSON.stringify(t.json, null, 2))}</pre>` : `<div class="gate">Nothing is published yet.</div>`)
      : t.rows.length ? `<div class="scroll"><table class="tbl"><thead><tr>${t.columns.map(c => `<th class="${c.fmt ? "num" : ""}">${esc(c.label)}</th>`).join("")}</tr></thead><tbody>${t.rows.map(r => `<tr>${t.columns.map(c => cell(c, r[c.key])).join("")}</tr>`).join("")}</tbody></table></div>` : `<div class="gate">No rows yet.</div>`}</div>`;
}

/* ---------------- system configuration ---------------- */
const SET = { data: null, sel: null, editing: false };
async function settingsView(uc) {
  $("#viewbody").innerHTML = `<div class="panel2 muted">Loading…</div>`;
  try { SET.data = await api(`/connections/${uc}?market=${MARKET}`); } catch (e) { $("#viewbody").innerHTML = `<div class="gate">${esc(e.message)}</div>`; return; }
  if (!SET.data.connections.some(c => c.id === SET.sel)) SET.sel = SET.data.connections[0].id;
  SET.editing = false;
  await drawSettings(uc);
}
async function drawSettings(uc) {
  const d = SET.data, c = d.connections.find(x => x.id === SET.sel), can = ME.role === WORK_ROLE[uc];
  let rec = []; if (c.type === "rest") rec = await api(`/connections/${uc}/${c.id}/receipts`).catch(() => []);
  if (modView() !== "settings") return;
  const st = c.last_ok === true ? chip("Connected", "ok") : c.last_ok === false ? chip("Failed", "bad") : chip("Not tested", "mute");
  const input = p => p.key === "token" ? `<input type="password" id="f_token" autocomplete="off" placeholder="Leave blank to keep the current token">` : p.key === "timeout_ms" ? `<input id="f_timeout_ms" type="number" min="500" max="30000" value="${esc(p.value)}">` : `<input id="f_${p.key}" value="${esc(p.value)}" maxlength="300">`;
  $("#viewbody").innerHTML = `<div class="panel2"><div class="row" style="gap:14px"><span class="what-n" style="background:var(--soft);color:var(--ink);border:1px solid var(--line);width:44px;height:44px;border-radius:12px">${ico("gear")}</span>
      <div><h2>System Configuration</h2><div class="small muted">Connection parameters for the systems this module talks to.</div></div><span class="right">${chip(`${d.connected} / ${d.total} systems connected`, d.connected === d.total ? "ok" : "mute")}</span></div>
    <div class="tabs" role="tablist" style="margin:20px 0 6px">${d.connections.map(x => `<button role="tab" aria-selected="${x.id === c.id}" data-conn="${x.id}"><span class="dot ${x.last_ok === true ? "ok" : x.last_ok === false ? "bad" : ""}"></span>${esc(x.label)}</button>`).join("")}</div>
    <div class="row" style="margin-top:10px"><h3>${esc(c.label)} — Connection parameters</h3><span class="right row">${st}${c.editable && can && !SET.editing ? `<button class="btn sm" id="c_edit">Edit</button>` : ""}<button class="btn sm primary" id="c_test" ${can ? "" : "disabled"}>${ico("play")} Test connection</button></span></div>
    <p class="muted" style="margin:8px 0 0">${esc(c.desc)}</p>
    <div class="params">${c.params.map(p => `<div><div class="pl">${esc(p.label)}</div><div class="pv">${SET.editing && p.key ? input(p) : esc(p.value)}</div></div>`).join("")}</div>
    ${SET.editing ? `<div class="row"><button class="btn primary" id="c_save">Save settings</button><button class="btn" id="c_cancel">Cancel</button></div>` : ""}
    ${c.last_message ? `<div class="gate" style="${c.last_ok === false ? "background:var(--rose-bg);color:var(--rose-ink)" : c.last_ok ? "background:var(--live-bg);color:var(--live-ink)" : ""}"><span><b>${c.last_ok === true ? "Test passed" : c.last_ok === false ? "Test failed" : "Note"}.</b> ${esc(c.last_message)}${c.last_tested ? ` <span class="small">Last tested ${esc(c.last_tested.replace("T", " ").replace("+00:00", " UTC"))}${c.last_ms != null ? `, ${c.last_ms} ms` : ""}.</span>` : ""}</span></div>` : ""}
    ${!can ? `<div class="gate">Only the ${esc(ROLE_LABEL[WORK_ROLE[uc]].toLowerCase())} can test or change connections. You can read them.</div>` : ""}
    ${c.type === "rest" ? `<h3 style="margin:22px 0 8px">Received by this system</h3>${rec.length ? `<div class="scroll"><table class="tbl"><thead><tr><th>Receipt</th><th>Kind</th><th>Reference</th><th>Received</th></tr></thead><tbody>${rec.map(r => `<tr><td><span class="cite">${esc(r.receipt)}</span></td><td>${esc(r.kind)}</td><td>${esc(r.ref)}</td><td class="small">${esc(r.received_at.replace("T", " ").replace("+00:00", " UTC"))}</td></tr>`).join("")}</tbody></table></div>` : `<p class="muted small" style="margin:0">Nothing received yet. ${esc(c.push || "")} from the agent page and it will appear here.</p>`}` : ""}
    <p class="small muted" style="margin:22px 0 0"><b>About this page:</b> The sandbox systems are part of this demo. They require a bearer token and keep what they receive, so a test and a send are real calls with real receipts. The server only connects to its own host or localhost. Nothing shows Connected until a test has passed.</p></div>`;
  $$("[data-conn]").forEach(b => b.onclick = () => { SET.sel = b.dataset.conn; SET.editing = false; drawSettings(uc); });
  const ed = $("#c_edit"); if (ed) ed.onclick = () => { SET.editing = true; drawSettings(uc); };
  const ca = $("#c_cancel"); if (ca) ca.onclick = () => { SET.editing = false; drawSettings(uc); };
  const sv = $("#c_save"); if (sv) sv.onclick = () => guard(async () => {
    const body = {}; for (const k of ["base_url", "environment", "timeout_ms", "token"]) { const e = $("#f_" + k); if (e && (e.value !== "" || k === "base_url")) body[k] = k === "timeout_ms" ? +e.value : e.value; }
    SET.data = await post(`/connections/${uc}/${c.id}?market=${MARKET}`, body); SET.editing = false; toast("Saved. Test the connection again."); drawSettings(uc);
  });
  const ts = $("#c_test"); if (ts) ts.onclick = () => guard(async () => { ts.disabled = true; ts.textContent = "Testing…"; SET.data = await post(`/connections/${uc}/${c.id}/test?market=${MARKET}`); drawSettings(uc); });
}

/* a push to a sandbox system (send configuration, post decision, queue letter), with a receipt */
async function pushTo(uc, refId, label) {
  const r = await post(`/push/${uc}?market=${MARKET}`, { ref_id: refId });
  toast(`${label}: receipt ${r.receipt} from ${r.system}.`);
  return r;
}
