"""JSON log lines: one object per record, structured `extra=` fields at the top level."""
from __future__ import annotations

import json
import logging
import sys

from salesbot.logs import JsonFormatter, setup_logging


def _record(**extra) -> logging.LogRecord:
    rec = logging.LogRecord("salesbot", logging.WARNING, __file__, 1, "order %s → %s", (7, "PAID"), None)
    rec.__dict__.update(extra)
    return rec


def test_json_line_has_message_and_structured_fields():
    line = json.loads(JsonFormatter().format(_record(event="order_status", order_id=7, chat_id=42)))
    assert line["msg"] == "order 7 → PAID" and line["level"] == "WARNING" and line["logger"] == "salesbot"
    assert line["event"] == "order_status" and line["order_id"] == 7 and line["chat_id"] == 42
    assert line["ts"].endswith("+00:00") and "args" not in line and "pathname" not in line


def test_exceptions_are_included():
    try:
        raise ValueError("boom")
    except ValueError:
        rec = logging.LogRecord("salesbot", logging.ERROR, __file__, 1, "failed", None, sys.exc_info())
    assert "ValueError: boom" in json.loads(JsonFormatter().format(rec))["exc"]


def test_setup_switches_the_formatter():
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    try:
        setup_logging("json")
        assert isinstance(root.handlers[0].formatter, JsonFormatter)
        setup_logging("text")
        assert not isinstance(root.handlers[0].formatter, JsonFormatter)
    finally:
        root.handlers[:], level = saved
        root.setLevel(level)
