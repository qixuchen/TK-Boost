"""The streaming LiteLLM adapter: reasoning controls and a wall-clock cap per call."""

import sys
import types

import pytest

from tkstore.dataclaw.reflector import LLMReply, litellm_llm


def _chunk(text, finish=None):
    choice = types.SimpleNamespace(delta=types.SimpleNamespace(content=text), finish_reason=finish)
    return types.SimpleNamespace(choices=[choice])


class FakeStream:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.closed = False

    def __iter__(self):
        return iter(self.chunks)

    def close(self):
        self.closed = True


@pytest.fixture
def fake_litellm(monkeypatch):
    state = {"calls": [], "stream": None}

    def completion(**kwargs):
        state["calls"].append(kwargs)
        return state["stream"]

    monkeypatch.setitem(sys.modules, "litellm", types.SimpleNamespace(completion=completion))
    return state


MESSAGES = [{"role": "user", "content": "hi"}]


def test_streams_and_joins_content(fake_litellm):
    fake_litellm["stream"] = FakeStream([_chunk("PLAN: x\n"), _chunk(None), _chunk("<probe>ls</probe>", "stop")])
    reply = litellm_llm("openai/glm-5.2")(MESSAGES)
    call = fake_litellm["calls"][0]
    assert call["stream"] is True and call["model"] == "openai/glm-5.2"
    assert reply == LLMReply("PLAN: x\n<probe>ls</probe>", "stop", False, reply.elapsed_s)


def test_no_reasoning_or_token_options_by_default(fake_litellm):
    fake_litellm["stream"] = FakeStream([_chunk("ok", "stop")])
    litellm_llm("m")(MESSAGES)
    call = fake_litellm["calls"][0]
    assert "max_tokens" not in call and "extra_body" not in call


def test_reasoning_controls_go_through_extra_body(fake_litellm):
    fake_litellm["stream"] = FakeStream([_chunk("ok", "stop")])
    litellm_llm("m", max_tokens=8192, reasoning_effort="low", disable_thinking=True)(MESSAGES)
    call = fake_litellm["calls"][0]
    assert call["max_tokens"] == 8192
    assert call["extra_body"] == {"reasoning_effort": "low", "thinking": {"type": "disabled"}}


def test_length_cut_with_no_text_is_reported(fake_litellm):
    fake_litellm["stream"] = FakeStream([_chunk(None), _chunk(None, "length")])
    reply = litellm_llm("m")(MESSAGES)
    assert reply.text == "" and reply.finish_reason == "length" and not reply.timed_out


def test_call_stops_at_the_wall_clock_cap(fake_litellm):
    stream = FakeStream([_chunk("a"), _chunk(None), _chunk(None), _chunk("never")])
    fake_litellm["stream"] = stream
    ticks = iter([0.0, 1.0, 5.0, 11.0, 20.0, 30.0])
    reply = litellm_llm("m", call_timeout_s=10, clock=lambda: next(ticks))(MESSAGES)
    assert reply.timed_out and reply.finish_reason == "timeout"
    assert reply.text == "a"
    assert stream.closed
    assert reply.elapsed_s == pytest.approx(11.0)
