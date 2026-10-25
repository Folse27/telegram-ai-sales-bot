"""The Claude call: cached system prompt, the `reply` tool, request parameters per model family, cost estimate."""
from __future__ import annotations

import copy
import json
import logging
import re
import time
from typing import Any

from .catalog import Catalog, clean_site_text
from .config import PROMPT_PATH, REPLY_TOOL_PATH, Settings
from .validation import reference_numbers

log = logging.getLogger("salesbot")

SAFE_PARAMS = {"forced": False, "max_tokens": 4000}  # accepted by every current model

# USD per million tokens (input, output). Cache reads cost 0.1× input; writes 1.25× (5 min) or 2× (1 h).
PRICES_PER_MTOK = {"claude-haiku-4-5": (1.0, 5.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-sonnet-5": (2.0, 10.0),
                   "claude-opus-5-5": (4.0, 20.0)}


def llm_call_cost(row: Any, cache_ttl: str = "1h") -> float:
    """Approximate USD cost of one llm_calls row."""
    model = next((m for m in PRICES_PER_MTOK if (row["model"] or "").startswith(m)), None)
    p_in, p_out = PRICES_PER_MTOK.get(model or "", (2.0, 10.0))
    write = p_in * (2.0 if cache_ttl == "1h" else 1.25)
    return ((row["input_tokens"] or 0) * p_in + (row["output_tokens"] or 0) * p_out
            + (row["cache_read_tokens"] or 0) * p_in * 0.1 + (row["cache_write_tokens"] or 0) * write) / 1e6


def model_params(model: str, effort: str = "low", thinking: str = "adaptive") -> dict:
    """Request differences per model family, checked against the model docs on 2026-10-01.
    forced: tool_choice {"type": "tool"} is allowed; temperature: sampling params accepted;
    thinking/effort: sent as given; max_tokens: floor when the model may think (thinking counts toward it)."""
    if re.match(r"claude-(haiku-4-5|3|sonnet-4-5|opus-4-5|opus-4-1)", model):
        return {"forced": True, "temperature": True}
    if re.match(r"claude-(sonnet-5|opus-5|opus-4-[678]|sonnet-4-6)(-\d{8})?$", model):  # exact ids, not the 5.5 ones
        return {"forced": True, "thinking": {"type": "disabled"}}  # forced tool use can't be combined with thinking
    if model.startswith("claude-sonnet-5-5"):  # no forced tool use, no "disabled", no temperature
        mode = "between_tools" if thinking == "between_tools" else "adaptive"
        return {"forced": False, "thinking": {"type": mode}, "effort": effort, "max_tokens": 4000}
    return {"forced": False, "effort": effort, "max_tokens": 4000}  # Opus 5.5, Fable, newer: thinking always on


def reply_tool(content: dict, max_parts: int) -> dict:
    """The `reply` tool schema with this shop's departments and the configured message limit."""
    tool = json.loads(REPLY_TOOL_PATH.read_text(encoding="utf-8").replace("{{max_parts}}", str(max_parts)))
    depts = list(content["departments"])
    part = tool["input_schema"]["properties"]["parts"]["items"]["properties"]
    part["department"]["anyOf"][0]["enum"] = depts
    tool["input_schema"]["properties"]["escalate"]["properties"]["department"]["enum"] = ["none", *depts]
    return tool


def cache_control(settings: Any) -> dict:
    """CACHE_TTL: 5m = default lifetime (write 1.25× input price); 1h = write 2×, survives pauses up to an hour."""
    if getattr(settings, "cache_ttl", "1h") == "1h":
        return {"type": "ephemeral", "ttl": "1h"}
    return {"type": "ephemeral"}


def build_system(settings: Any, catalog: Catalog, content: dict, examples: str) -> list[dict]:
    """Two static system blocks; the second ends the cached prefix (tools + system). Nothing volatile in here."""
    raw = PROMPT_PATH.read_text(encoding="utf-8").replace("{{examples}}", examples.strip())
    rules, reference = raw.split("<!-- REFERENCE -->", 1)
    deps = []
    for key, d in content["departments"].items():
        contacts = ", ".join(v for v in (d.get("phone"), d.get("email"), d.get("url")) if v)
        deps.append(f"- {key}: {d['title_uk']} — {contacts}" + (f" ({d['note_uk']})" if d.get("note_uk") else ""))
    vat = [clean_site_text(catalog.vat_policy["without_vat_condition_uk"])] + [
        f"- {f['q']} {clean_site_text(f['a'])}" for f in catalog.vat_policy["faq"]]
    values = {
        "persona_name": settings.persona_name,
        "company": content["persona"]["company"],
        "company_intro": content["persona"]["intro_en"],
        "max_parts": str(getattr(settings, "max_parts", 3)),
        "catalog_version": catalog.version,
        "catalog_index": catalog.index_lines(),
        "departments": "\n".join(deps),
        "services": "\n".join(f"- {s}" for s in content["services_uk"]),
        "policies": "\n".join(f"- {v}" for v in content["policies_uk"].values()),
        "vat_policy": "\n".join(vat),
    }
    for k, v in values.items():
        rules, reference = rules.replace("{{" + k + "}}", v), reference.replace("{{" + k + "}}", v)
    return [{"type": "text", "text": rules.strip()},
            {"type": "text", "text": reference.strip(), "cache_control": cache_control(settings)}]


class LLM:
    def __init__(self, settings: Settings, catalog: Catalog, content: dict, examples: str):
        import anthropic  # imported here so unit tests of other modules don't need the SDK configured

        self.anthropic = anthropic
        self.settings = settings
        self.client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key or None,
                                               max_retries=2, timeout=settings.llm_timeout)
        self.tool = reply_tool(content, settings.max_parts)
        self.system = build_system(settings, catalog, content, examples)
        self.reference_numbers = reference_numbers(self.system)
        self.params = model_params(settings.model, settings.llm_effort, settings.llm_thinking)

    def request(self, messages: list[dict]) -> dict:
        p = self.params
        req: dict[str, Any] = {
            "model": self.settings.model, "system": self.system, "tools": [self.tool], "messages": messages,
            "max_tokens": max(self.settings.llm_max_tokens, p.get("max_tokens", 0)),
            "tool_choice": {"type": "tool", "name": "reply"} if p.get("forced") else {"type": "auto"}}
        if p.get("temperature"):  # SDK 1.x dropped the kwarg; models before 4.6 (Haiku 4.5) still honour it
            req["extra_body"] = {"temperature": self.settings.llm_temperature}
        if p.get("thinking"):
            req["thinking"] = p["thinking"]
        if p.get("effort"):
            req["output_config"] = {"effort": p["effort"]}
        return req

    async def call(self, chat_id: int, messages: list[dict], attempt: int) -> tuple[Any, dict | None, int]:
        t0 = time.monotonic()
        try:
            resp = await self.client.messages.create(**self.request(messages))
        except self.anthropic.BadRequestError as e:
            # A model that rejects one of our per-family params (a new model id, changed rules): fall back to the
            # request every current model accepts, keep it for the rest of the process, and retry once.
            if self.params == SAFE_PARAMS or not re.search(r"tool_choice|thinking|temperature|effort|output_config",
                                                           str(e)):
                raise
            log.warning("model %s rejected request params (%s); switching to safe params", self.settings.model, e)
            self.params = copy.deepcopy(SAFE_PARAMS)
            resp = await self.client.messages.create(**self.request(messages))
        block = next((b for b in resp.content if b.type == "tool_use" and b.name == "reply"), None)
        latency = int((time.monotonic() - t0) * 1000)
        u = resp.usage
        log.info("llm chat=%s attempt=%d %dms in=%s out=%s cache_read=%s cache_write=%s stop=%s", chat_id, attempt,
                 latency, u.input_tokens, u.output_tokens, u.cache_read_input_tokens, u.cache_creation_input_tokens,
                 resp.stop_reason,
                 extra={"event": "llm_call", "chat_id": chat_id, "attempt": attempt, "latency_ms": latency,
                        "model": self.settings.model, "input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                        "cache_read_tokens": u.cache_read_input_tokens, "cache_write_tokens": u.cache_creation_input_tokens,
                        "stop_reason": resp.stop_reason})
        if resp.stop_reason in ("refusal", "max_tokens"):  # unusable: treated like a missing reply
            block = None
        return resp, (block and {"id": block.id, "input": block.input}), latency
