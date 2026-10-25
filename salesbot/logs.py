"""Logging setup: readable text locally, one JSON object per line in production (LOG_FORMAT=json).

Structured fields go in through `extra=`, e.g. log.info("llm call", extra={"chat_id": 1, "latency_ms": 900}),
and become top-level JSON keys, so a log service can filter and chart them. Message texts of clients are never
logged; only ids, counts, timings and statuses.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

# Attributes every LogRecord has; anything else on a record came in through `extra=`.
_STANDARD = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        out.update({k: v for k, v in record.__dict__.items() if k not in _STANDARD and not k.startswith("_")})
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False, default=str)


def setup_logging(fmt: str = "text", level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
