/* Shared agent-card helpers for the Claims and Denials pages: the recorded log, the Run agent button, and the step-by-step reveal. */
const AG = { running: null, reveal: null };

function logHtml(key, log) {
  const hide = AG.reveal === key ? ' style="display:none"' : "";
  return `<div class="log" data-log="${key}" aria-label="Agent log">${log.map(s => `<div class="logstep ${s.status}"${hide}><span class="ic">${s.status === "ok" ? "✓" : s.status === "warn" ? "!" : "i"}</span><div><div class="t">${esc(s.step)}</div><div class="d">${esc(s.detail)}</div></div><span class="ms">${s.ms} ms</span></div>`).join("")}</div>`;
}
function runButton(key, enabled, label) {
  const running = AG.running === key;
  return `<button class="btn primary runbtn" data-run="${key}" ${enabled && !AG.running ? "" : "disabled"} aria-label="Run ${esc(label)}">${running ? `<span class="spin"></span> Agent working…` : `${ico("play")} Run agent`}</button>`;
}
const resultsAttr = key => AG.reveal === key ? ' style="display:none"' : "";
async function animateReveal(key) {
  const steps = $$(`[data-log="${key}"] .logstep`), res = $(`[data-results="${key}"]`);
  for (const s of steps) { s.style.display = ""; s.classList.add("reveal"); await sleep(420); }
  if (res) { res.style.display = ""; res.classList.add("reveal"); }
  AG.reveal = null;
}
/* Runs one agent: shows the spinner at least 800 ms, then lets the log appear step by step before the results. */
async function runAgentWith(key, call, render) {
  AG.running = key; render();
  const started = Date.now();
  try {
    await call();
    const wait = 800 - (Date.now() - started);
    if (wait > 0) await sleep(wait);
    AG.reveal = key;
  } catch (e) { toast(e.message, true); }
  finally { AG.running = null; render(); if (AG.reveal) animateReveal(AG.reveal); }
}
const ev = (ok, t) => `<span class="ev ${ok ? "ok" : "bad"}">${ico(ok ? "check" : "x")} ${esc(t)}</span>`;
const stepper = (steps, states) => `<div class="panel2"><div class="rail" style="grid-template-columns:repeat(${steps.length},1fr)" role="list">${states.map((st, i) =>
  `<div class="step ${st.done ? "done" : ""} ${st.cur ? "cur" : ""}" role="listitem"><span class="n">${st.done ? "✓" : i + 1}</span><b>${steps[i][0]}</b><span class="d">${steps[i][1]}</span></div>`).join("")}</div></div>`;
function stepStates(done) { const cur = done.findIndex(d => !d); return done.map((d, i) => ({ done: d, cur: i === (cur === -1 ? done.length - 1 : cur) && !d })); }
