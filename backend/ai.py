"""AI service layer: Gemini over REST, with a labelled rule-based fallback in the callers.

STATUS: written from Google's published generateContent reference. It has not been run against a live key
in this build until GEMINI_API_KEY is set (see config.py and scripts/check_gemini.py).
The key travels in the x-goog-api-key header, never in the URL, so it cannot leak through logs or error messages.
Locally it comes from .env; on Cloud Run from Secret Manager.
"""
import base64
import json
import logging
import os
import re
import urllib.request

import config  # noqa: F401  (loads .env)

log = logging.getLogger("payer.ai")
BASE = "https://generativelanguage.googleapis.com/v1beta"
_model_cache: dict = {}
LAST_ERROR: dict = {"message": None}


def available() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY"))


def _headers(key: str) -> dict:
    return {"Content-Type": "application/json", "x-goog-api-key": key}


def _get(url: str, key: str, timeout: int = 20):
    with urllib.request.urlopen(urllib.request.Request(url, headers=_headers(key)), timeout=timeout) as resp:
        return json.load(resp)


def model() -> str:
    """GEMINI_MODEL if set, otherwise the newest Flash-class model this key can use (discovered, never guessed)."""
    if os.environ.get("GEMINI_MODEL"):
        return os.environ["GEMINI_MODEL"]
    if "name" in _model_cache:
        return _model_cache["name"]
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return "gemini (no key)"
    try:
        models = _get(f"{BASE}/models?pageSize=200", key).get("models", [])
        ok = [m["name"].split("/", 1)[1] for m in models
              if "generateContent" in m.get("supportedGenerationMethods", []) and "flash" in m["name"]
              and not re.search(r"lite|image|tts|live|audio|embed|thinking|exp|preview", m["name"])]
        ok.sort(key=lambda n: [int(x) for x in re.findall(r"\d+", n)], reverse=True)
        _model_cache["name"] = ok[0] if ok else "gemini-flash-latest"
    except Exception as exc:  # noqa: BLE001
        LAST_ERROR["message"] = f"model discovery failed ({type(exc).__name__})"
        _model_cache["name"] = "gemini-flash-latest"
    return _model_cache["name"]


def status() -> dict:
    live = available()
    return {"provider": "Gemini", "model": model() if live else None, "live": live,
            "last_error": LAST_ERROR["message"],
            "note": "Live Gemini agents." if live else "No GEMINI_API_KEY: agents use the labelled rule-based fallback."}


def _call(system: str, parts: list, as_json: bool, timeout: int = 60):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": parts}]}
    if as_json:
        body["generationConfig"] = {"responseMimeType": "application/json", "temperature": 0}
    else:
        body["generationConfig"] = {"temperature": 0.2}
    req = urllib.request.Request(f"{BASE}/models/{model()}:generateContent", data=json.dumps(body).encode(),
                                 headers=_headers(key), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
        text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
        LAST_ERROR["message"] = None
        return json.loads(text) if as_json else text
    except Exception as exc:  # noqa: BLE001 - any failure means the caller uses its fallback
        LAST_ERROR["message"] = f"{type(exc).__name__} on the last Gemini call"
        log.warning("Gemini call failed (%s)", type(exc).__name__)
        return None


def generate_json(system: str, user: str) -> dict | None:
    out = _call(system, [{"text": user}], True)
    return out if isinstance(out, dict) else None


def generate_text(system: str, user: str) -> str | None:
    out = _call(system, [{"text": user}], False)
    return out.strip() if isinstance(out, str) and out.strip() else None


def transcribe(data: bytes, mime: str, what: str = "plan") -> str | None:
    """Read a PDF or image into plain text (multimodal). Everything extracted afterwards is verified against this text."""
    part = {"inlineData": {"mimeType": mime, "data": base64.b64encode(data).decode()}}
    if what == "bill":
        system = ("Transcribe this hospital bill or claim document into plain text, faithfully and completely. "
                  "Write each charge on its own line as: item no | item code (blank if none) | description | quantity | date YYYY-MM-DD | amount. "
                  "Keep header details (patient, member id, dates, procedure, authorization reference, total) as 'Label: value' lines. "
                  "Do not summarise, interpret or add anything.")
    else:
        system = ("Transcribe this insurance plan document into plain text, faithfully and completely. "
                  "Start each page with a line '[Page N]'. Keep clause or section numbers at the start of each clause. "
                  "Do not summarise, interpret or add anything.")
    out = _call(system, [part, {"text": "Transcribe the document."}], False, timeout=120)
    return out.strip() if isinstance(out, str) and out.strip() else None
