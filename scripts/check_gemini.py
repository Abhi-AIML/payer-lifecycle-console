"""Checks the Gemini key from .env: lists the discovered model and makes one tiny call. Prints no secrets."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import ai  # noqa: E402

print("key present:", ai.available())
if ai.available():
    print("model:", ai.model())
    out = ai.generate_json("Reply with JSON only.", 'Return {"ok": true, "word": "ready"}')
    print("live call:", out if out else f"FAILED ({ai.LAST_ERROR['message']})")
