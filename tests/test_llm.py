"""Request shape per model, the parameter fallback, cost estimate, tool schema and configuration checks."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from conftest import CAT, CONTENT, SHOP, settings
from pydantic import ValidationError

from salesbot.config import Settings, describe
from salesbot.llm import LLM, build_system, llm_call_cost, reply_tool


def _llm(model, thinking="adaptive"):
    return LLM(settings(model=model, llm_thinking=thinking), CAT, CONTENT, SHOP.examples)


def test_request_shape_per_model():
    haiku = _llm("claude-haiku-4-5").request([])
    assert haiku["tool_choice"] == {"type": "tool", "name": "reply"} and haiku["extra_body"] == {"temperature": 0.2}
    assert "thinking" not in haiku and "output_config" not in haiku and haiku["max_tokens"] == 1200
    s55 = _llm("claude-sonnet-5-5").request([])  # docs: no forced tool use, no "disabled", no temperature
    assert s55["tool_choice"] == {"type": "auto"} and s55["thinking"] == {"type": "adaptive"}
    assert s55["output_config"] == {"effort": "low"} and s55["max_tokens"] == 4000 and "extra_body" not in s55
    assert _llm("claude-sonnet-5-5", "between_tools").request([])["thinking"] == {"type": "between_tools"}
    s5 = _llm("claude-sonnet-5").request([])
    assert s5["tool_choice"]["type"] == "tool" and s5["thinking"] == {"type": "disabled"}
    o55 = _llm("claude-opus-5-5").request([])
    assert o55["tool_choice"] == {"type": "auto"} and "thinking" not in o55 and o55["output_config"] == {"effort": "low"}


async def test_rejected_params_fall_back_to_safe_ones():
    class FakeBadRequest(Exception):
        pass

    llm = _llm("claude-sonnet-5-5")
    llm.anthropic = SimpleNamespace(BadRequestError=FakeBadRequest)
    seen = []

    async def create(**req):
        seen.append(req)
        if len(seen) == 1:
            raise FakeBadRequest("thinking: unsupported value")
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name="reply", id="t1", input={"parts": []})],
                               stop_reason="tool_use", usage=SimpleNamespace(input_tokens=1, output_tokens=1,
                               cache_read_input_tokens=0, cache_creation_input_tokens=0))

    llm.client = SimpleNamespace(messages=SimpleNamespace(create=create))
    _, call, _ = await llm.call(1, [], 1)
    assert call["id"] == "t1" and "thinking" not in seen[1] and seen[1]["tool_choice"] == {"type": "auto"}


def test_cache_ttl_setting():
    one_hour = build_system(SimpleNamespace(persona_name="Микола", cache_ttl="1h"), CAT, CONTENT, SHOP.examples)
    assert one_hour[1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    five_min = build_system(SimpleNamespace(persona_name="Микола", cache_ttl="5m"), CAT, CONTENT, SHOP.examples)
    assert five_min[1]["cache_control"] == {"type": "ephemeral"}
    with pytest.raises(ValidationError):
        settings(cache_ttl="10m")


def test_llm_call_cost():
    row = {"model": "claude-sonnet-5-5", "input_tokens": 1000, "output_tokens": 500,
           "cache_read_tokens": 15000, "cache_write_tokens": 0}
    assert llm_call_cost(row) == pytest.approx((1000 * 2 + 500 * 10 + 15000 * 0.2) / 1e6)
    row = {**row, "model": "claude-haiku-4-5-20251001", "cache_read_tokens": 0, "cache_write_tokens": 10000}
    assert llm_call_cost(row, "5m") == pytest.approx((1000 + 2500 + 10000 * 1.25) / 1e6)


def test_reply_tool_uses_the_shop_departments_and_message_limit():
    tool = reply_tool(CONTENT, 2)
    props = tool["input_schema"]["properties"]
    assert props["parts"]["items"]["properties"]["department"]["anyOf"][0]["enum"] == list(CONTENT["departments"])
    assert props["escalate"]["properties"]["department"]["enum"][0] == "none"
    assert "1 to 2 messages" in props["parts"]["description"]


def test_missing_or_blank_settings_fail_with_a_clear_message():
    with pytest.raises(ValidationError) as e:
        Settings(_env_file=None, telegram_bot_token="1:x", webhook_secret="x", anthropic_api_key=" ")  # type: ignore[call-arg]
    text = describe(e.value)
    assert "DATABASE_URL: missing" in text and "ADMIN_CHAT_ID: missing" in text and "ANTHROPIC_API_KEY" in text
    with pytest.raises(ValidationError):
        settings(max_parts=6)
    assert settings(tester_ids="1, 2").tester_ids == [1, 2]
    assert settings().shop_dir is not None and settings(shop_source="db").shop_dir is None
