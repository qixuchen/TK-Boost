"""Command-line defaults of scripts/dataclaw_reflect.py and the LLMs it builds."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "dataclaw_reflect.py"


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location("dataclaw_reflect", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _llms(script, *argv):
    calls = []

    def factory(model, **options):
        calls.append((model, options))
        return model

    script.make_llms(script.build_parser().parse_args(list(argv)), factory=factory)
    return calls


def test_reasoning_effort_defaults_to_low(script):
    calls = _llms(script, "--model", "m")
    assert calls[0][1]["reasoning_effort"] == "low"


def test_reasoning_effort_can_be_left_to_the_gateway(script):
    calls = _llms(script, "--model", "m", "--reasoning-effort", "omit")
    assert calls[0][1]["reasoning_effort"] is None


def test_reasoning_effort_none_is_sent_as_is(script):
    calls = _llms(script, "--model", "m", "--reasoning-effort", "none")
    assert calls[0][1]["reasoning_effort"] == "none"


def test_judge_model_gets_the_same_reasoning_options(script):
    calls = _llms(script, "--model", "m", "--judge-model", "j", "--max-tokens", "9000")
    assert calls[1] == ("j", calls[0][1])


def test_judge_defaults_to_the_reflector_llm(script):
    calls = _llms(script, "--model", "m")
    assert [model for model, _ in calls] == ["m"]
