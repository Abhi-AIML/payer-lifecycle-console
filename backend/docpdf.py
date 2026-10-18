"""Documents as real PDFs, and pages rendered for the viewer with the cited clause highlighted.

A policy document becomes a PDF in which every "[Page N]" marker starts PDF page N, so the page a rule cites is the page you see.
The rectangle of every numbered clause is recorded while the page is laid out, which is what the viewer highlights.
An uploaded PDF is kept as uploaded; for it the highlight is a best-effort text search.
"""
import re

import pymupdf

W, H, M = 595, 842, 56
BODY, HEAD, TITLE = 10.5, 12.5, 16
INK, MUTED, BLUE = (0.05, 0.1, 0.22), (0.38, 0.43, 0.52), (0.11, 0.36, 0.94)
_cache: dict = {}


def _clean(s: str) -> str:
    return s.replace("₹", "Rs.").replace("–", "-").replace("—", "-").replace("’", "'").replace("“", '"').replace("”", '"').encode("latin-1", "replace").decode("latin-1")


def _wrap(text: str, font: str, size: float, width: float) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if pymupdf.get_text_length(trial, fontname=font, fontsize=size) <= width:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    return lines + ([cur] if cur else [])


def _footer(page, n: int, total: int, label: str) -> None:
    page.draw_line((M, H - 44), (W - M, H - 44), color=(0.82, 0.85, 0.9), width=0.6)
    page.insert_text((M, H - 30), f"{label}  ·  Page {n} of {total}  ·  Synthetic document for demonstration", fontname="helv", fontsize=8, color=MUTED)


def policy_pdf(text: str) -> tuple[bytes, dict]:
    """Returns (pdf bytes, {clause: {"page": n, "rect": [x0, y0, x1, y1]}}). Each [Page N] marker starts a new PDF page."""
    key = ("policy", hash(text))
    if key in _cache:
        return _cache[key]
    text = _clean(text)
    parts = re.split(r"^\[Page (\d+)\]\s*", text, flags=re.M)
    head, pages = [x.strip() for x in parts[0].strip().splitlines() if x.strip()], list(zip(parts[1::2], parts[2::2]))
    doc, layout = pymupdf.open(), {}
    for idx, (num, body) in enumerate(pages, 1):
        page, y = doc.new_page(width=W, height=H), 64.0
        if idx == 1 and head:
            for ln in _wrap(head[0], "hebo", TITLE, W - 2 * M):
                page.insert_text((M, y), ln, fontname="hebo", fontsize=TITLE, color=INK)
                y += TITLE + 5
            for sub in head[1:]:
                for ln in _wrap(sub, "helv", 9.5, W - 2 * M):
                    page.insert_text((M, y), ln, fontname="helv", fontsize=9.5, color=MUTED)
                    y += 13
            y += 14
        lines = [x.strip() for x in body.strip().splitlines() if x.strip()]
        for ln in lines:
            m = re.match(r"^(\d+(?:\.\d+)+)\s+(.*)$", ln)
            if not m:  # section heading
                y += 8
                for w in _wrap(ln, "hebo", HEAD, W - 2 * M):
                    page.insert_text((M, y), w, fontname="hebo", fontsize=HEAD, color=INK)
                    y += HEAD + 5
                continue
            cid, txt = m.group(1), m.group(2)
            indent = 38
            wrapped = _wrap(txt, "helv", BODY, W - 2 * M - indent)
            top = y - BODY - 1
            page.insert_text((M, y), cid, fontname="hebo", fontsize=BODY, color=BLUE)
            for w in wrapped:
                page.insert_text((M + indent, y), w, fontname="helv", fontsize=BODY, color=INK)
                y += BODY + 4.5
            layout[cid] = {"page": idx, "rect": [M - 6, top - 2, W - M + 6, y - 2]}
            y += 7
        _footer(page, idx, len(pages), head[0][:60] if head else "Policy document")
    out = (doc.tobytes(), layout)
    _cache[key] = out
    return out


def text_pdf(text: str, label: str = "Document") -> bytes:
    """A claim document (bill, discharge summary, approval letter) as a PDF. 'Label: value' lines and pipe tables get real layout."""
    key = ("text", hash(text), label)
    if key in _cache:
        return _cache[key]
    text = _clean(text)
    doc = pymupdf.open()
    page, y = doc.new_page(width=W, height=H), 64.0
    lines = [x.rstrip() for x in text.splitlines() if x.strip()]
    cols = [(M, "Item"), (84, "Code"), (198, "Description"), (352, "Qty"), (384, "Date"), (W - M, "Amount")]

    def room(need: float = 40):
        nonlocal page, y
        if y > H - 70 - need:
            page, y = doc.new_page(width=W, height=H), 64.0

    for k, ln in enumerate(lines):
        room()
        if k == 0:
            for w in _wrap(ln, "hebo", 14, W - 2 * M):
                page.insert_text((M, y), w, fontname="hebo", fontsize=14, color=INK)
                y += 19
            page.draw_line((M, y - 8), (W - M, y - 8), color=BLUE, width=1.2)
            y += 8
            continue
        cells = [c.strip() for c in ln.split("|")]
        if len(cells) >= 6 and cells[0].isdigit():
            for i, ((x, _), c) in enumerate(zip(cols, cells)):
                if i == 5:
                    page.insert_text((W - M - pymupdf.get_text_length(c, fontname="helv", fontsize=BODY), y), c, fontname="helv", fontsize=BODY, color=INK)
                else:
                    page.insert_text((x, y), c[:34], fontname="helv", fontsize=BODY, color=INK)
            page.draw_line((M, y + 5), (W - M, y + 5), color=(0.9, 0.92, 0.95), width=0.5)
            y += 19
            continue
        if ln.upper().startswith("ITEMS"):
            y += 8
            page.draw_rect(pymupdf.Rect(M - 4, y - 12, W - M + 4, y + 6), color=None, fill=(0.93, 0.95, 0.99))
            for (x, name) in cols:
                if name == "Amount":
                    page.insert_text((W - M - pymupdf.get_text_length(name, fontname="hebo", fontsize=9), y), name, fontname="hebo", fontsize=9, color=MUTED)
                else:
                    page.insert_text((x, y), name, fontname="hebo", fontsize=9, color=MUTED)
            y += 22
            continue
        m = re.match(r"^([A-Za-z][A-Za-z ./-]{1,34}):\s*(.*)$", ln)
        if m:
            bold = m.group(1).lower().startswith("total")
            page.insert_text((M, y), m.group(1) + ":", fontname="hebo", fontsize=BODY, color=MUTED)
            x = M + 150
            for w in _wrap(m.group(2), "hebo" if bold else "helv", BODY, W - M - x) or [""]:
                page.insert_text((x, y), w, fontname="hebo" if bold else "helv", fontsize=BODY, color=INK)
                y += 15
            continue
        for w in _wrap(ln, "helv", BODY, W - 2 * M):
            page.insert_text((M, y), w, fontname="helv", fontsize=BODY, color=INK)
            y += 15
    for i, pg in enumerate(doc, 1):
        _footer(pg, i, len(doc), label)
    out = doc.tobytes()
    _cache[key] = out
    return out


def page_count(pdf: bytes) -> int:
    return len(pymupdf.open("pdf", pdf))


def find_rects(pdf: bytes, page_no: int, quote: str) -> list:
    """Best-effort text search, used when the PDF was uploaded and there is no layout record."""
    doc = pymupdf.open("pdf", pdf)
    if not 1 <= page_no <= len(doc) or not quote:
        return []
    page, q = doc[page_no - 1], re.sub(r"\s+", " ", quote).strip()
    for n in (len(q), 90, 60, 36):
        hits = page.search_for(q[:n])
        if hits:
            return [pymupdf.Rect(hits[0].x0 - 3, min(r.y0 for r in hits) - 2, max(r.x1 for r in hits) + 3, max(r.y1 for r in hits) + 2)]
    return []


def render_page(pdf: bytes, page_no: int, rects=None, zoom: float = 1.9) -> bytes:
    doc = pymupdf.open("pdf", pdf)
    if not 1 <= page_no <= len(doc):
        raise LookupError("Page not found.")
    page = doc[page_no - 1]
    for r in rects or []:
        rect = pymupdf.Rect(*r)
        page.draw_rect(rect, color=None, fill=(1.0, 0.9, 0.45), overlay=False)  # behind the text, so the words stay crisp
        page.draw_rect(rect, color=(0.95, 0.65, 0.1), width=1.2)
    return page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).tobytes("png")
