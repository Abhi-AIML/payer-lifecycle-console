"""Turns plan-document text into pages and numbered clauses, so every rule can cite where it came from."""
import re


def parse_document(text: str) -> dict:
    page, clauses, header, n = 1, [], [], 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^\[page\s+(\d+)\]\s*(.*)$", line, re.I)
        if m:
            page = int(m.group(1))
            continue
        if re.match(r"^section\s+\d+", line, re.I):
            continue
        m = re.match(r"^(\d+(?:\.\d+)+)\s+(.+)$", line)
        if m:
            clauses.append({"page": page, "clause": m.group(1), "text": m.group(2)})
            continue
        if not clauses and page == 1:
            header.append(line)
            continue
        n += 1
        clauses.append({"page": page, "clause": f"p{page}.{n}", "text": line})
    head = " ".join(header)
    eff = re.search(r"effective\s+(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})", head + " " + text[:600], re.I)
    ver = re.search(r"version\s+(\d+)", head, re.I)
    return {
        "title": header[0] if header else "Untitled document",
        "effective": eff.group(1) if eff else None,
        "version": int(ver.group(1)) if ver else 1,
        "is_amendment": bool(re.search(r"amendment", head, re.I)),
        "clauses": clauses,
        "pages": max([c["page"] for c in clauses] or [1]),
    }


def norm(s: str) -> str:
    s = (s or "").replace("’", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", s).strip().lower()
