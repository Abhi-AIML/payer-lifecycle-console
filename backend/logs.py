"""Structured logging for Cloud Logging.

On Cloud Run every line written to stdout is collected by Cloud Logging. A line that is a JSON object is stored as a
structured entry: "severity" sets the log level and "logging.googleapis.com/labels" become filterable labels.
So the audit trail outlives the in-memory state: filter on labels.kind="audit" in the Logs Explorer.

Locally the same records print as plain text. Never log secrets, document text or the Gemini request URL.
"""
import json
import logging
import sys

import config

AUDIT = logging.getLogger("payer.audit")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {"severity": record.levelname, "message": record.getMessage(), "logger": record.name}
        entry.update(getattr(record, "fields", {}))
        if record.exc_info:
            entry["stack_trace"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def setup() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if config.ON_CLOUD_RUN else logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(config.LOG_LEVEL)


def audit_event(event: dict) -> None:
    """One audit entry (already built by service.audit) as a labelled log line."""
    AUDIT.info(f"{event['action']}: {event['detail']}".rstrip(": "),
               extra={"fields": {"audit": event, "logging.googleapis.com/labels": {"kind": "audit", "action": event["action"]}}})
