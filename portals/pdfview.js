/* Full-size document viewer. Pages are rendered by the server from the real PDF, so what you see is what the PDF holds.
   open({ title, pdfUrl, pages, page, clause, pageUrl: (n, clause) => url }) opens at a page; a clause is highlighted on its own page. */
const PDFV = {
  o: null, page: 1,
  open(o) { PDFV.o = o; PDFV.page = o.page || 1; PDFV.draw(); },
  close() { const m = document.getElementById("pdfmodal"); if (m) m.remove(); document.body.style.overflow = ""; document.removeEventListener("keydown", PDFV.key); },
  key(e) { if (e.key === "Escape") PDFV.close(); else if (e.key === "ArrowRight") PDFV.go(1); else if (e.key === "ArrowLeft") PDFV.go(-1); },
  go(d) { const n = PDFV.page + d; if (PDFV.o && n >= 1 && n <= PDFV.o.pages) { PDFV.page = n; PDFV.draw(); } },
  draw() {
    const o = PDFV.o, n = PDFV.page;
    let m = document.getElementById("pdfmodal");
    if (!m) { m = document.createElement("div"); m.id = "pdfmodal"; m.className = "pdfmodal"; m.setAttribute("role", "dialog"); m.setAttribute("aria-modal", "true"); document.body.appendChild(m); document.body.style.overflow = "hidden"; document.addEventListener("keydown", PDFV.key); }
    m.setAttribute("aria-label", o.title);
    const hl = o.clause && n === o.page ? o.clause : null;
    m.innerHTML = `<div class="pdfbox"><div class="pdfbar"><b class="pdftitle">${esc(o.title)}</b>
      ${o.clause ? chip(hl ? `Highlighted: clause ${o.clause}, page ${o.page}` : `Cited: clause ${o.clause}, page ${o.page}`, hl ? "warn" : "mute") : ""}
      <span class="right row" style="gap:8px"><button class="btn sm" id="pdfprev" ${n <= 1 ? "disabled" : ""}>‹ Prev</button><span class="small">Page <b>${n}</b> of ${o.pages}</span><button class="btn sm" id="pdfnext" ${n >= o.pages ? "disabled" : ""}>Next ›</button>
      <a class="btn sm" href="${o.pdfUrl}" target="_blank" rel="noopener">Open PDF</a><a class="btn sm" href="${o.pdfUrl}" download>Download</a><button class="btn sm primary" id="pdfclose">Close</button></span></div>
      <div class="pdfscroll"><img class="pdfimg" alt="Page ${n} of ${esc(o.title)}" src="${o.pageUrl(n, hl)}"></div></div>`;
    $("#pdfprev").onclick = () => PDFV.go(-1); $("#pdfnext").onclick = () => PDFV.go(1); $("#pdfclose").onclick = PDFV.close;
    m.onclick = e => { if (e.target === m) PDFV.close(); };
  }
};
