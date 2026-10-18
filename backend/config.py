"""Runtime settings, read once at startup. Never prints values.

Locally, the .env file in the project root is loaded into the environment first.
On Cloud Run there is no .env (it is never copied into the image): secrets arrive as environment variables
mounted from Secret Manager, and plain settings are set on the service by the deploy pipeline.
"""
import os
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def load_env() -> bool:
    if not ENV_FILE.exists():
        return False
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if key.strip() and value and key.strip() not in os.environ:
            os.environ[key.strip()] = value
    return True


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


load_env()

# Cloud Run sets K_SERVICE on every instance; nothing else needs to tell the app where it runs.
ON_CLOUD_RUN = bool(os.environ.get("K_SERVICE"))

# FastAPI's /docs and /openapi.json describe every endpoint. Useful locally, not published on the live demo.
API_DOCS = _flag("API_DOCS", not ON_CLOUD_RUN)

# The sign-in page offers one-click demo accounts. Set to false when the password is handed to judges privately.
SHOW_DEMO_PASSWORD = _flag("SHOW_DEMO_PASSWORD", True)

# Agent runs per account per window. Protects the Gemini free-tier quota on a public demo.
AGENT_RUNS_PER_WINDOW = _int("AGENT_RUNS_PER_WINDOW", 30)
AGENT_WINDOW_SECONDS = _int("AGENT_WINDOW_SECONDS", 300)

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO" if ON_CLOUD_RUN else "WARNING").upper()
