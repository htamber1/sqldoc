"""Multi-backend AI dispatch: ollama / anthropic / openai / gemini."""
import sys
import types

import pytest

from sqldoc import ai


@pytest.fixture(autouse=True)
def _reset_backend():
    ai.set_backend(None)
    yield
    ai.set_backend(None)


# --- backend resolution -----------------------------------------------------

def test_resolve_backend_from_mode():
    assert ai.resolve_backend("local") == "ollama"
    assert ai.resolve_backend("cloud") == "anthropic"


def test_set_backend_override_wins():
    ai.set_backend("openai")
    assert ai.resolve_backend("local") == "openai"
    assert ai.resolve_backend("cloud") == "openai"


def test_explicit_arg_beats_override():
    ai.set_backend("openai")
    assert ai.resolve_backend("local", backend="gemini") == "gemini"


def test_set_backend_rejects_unknown():
    with pytest.raises(ValueError):
        ai.set_backend("grok")


def test_default_model_per_backend():
    assert ai.default_model("ollama") == "llama3.1:8b"
    assert ai.default_model("anthropic") == "claude-haiku-4-5"
    assert ai.default_model("openai") == "gpt-4o"
    assert ai.default_model("gemini") == "gemini-1.5-flash"


def test_is_cloud_backend():
    assert ai.is_cloud_backend("cloud") is True
    assert ai.is_cloud_backend("local") is False
    ai.set_backend("gemini")
    assert ai.is_cloud_backend("local") is True   # override forces cloud
    assert ai.is_cloud_backend("cloud", backend="ollama") is False


# --- dispatch routes to the right backend ----------------------------------

def test_dispatch_routes_ollama(monkeypatch):
    seen = {}
    monkeypatch.setattr(ai, "_call_ollama", lambda p, m: seen.setdefault("ollama", (p, m)) or "x")
    ai.dispatch("hi", mode="local")
    assert seen["ollama"][1] == "llama3.1:8b"


def test_dispatch_routes_anthropic(monkeypatch):
    seen = {}
    monkeypatch.setattr(ai, "_call_anthropic", lambda p, m, mt=200: seen.setdefault("a", (p, m, mt)) or "x")
    ai.dispatch("hi", mode="cloud", max_tokens=700)
    assert seen["a"][1] == "claude-haiku-4-5" and seen["a"][2] == 700


def test_dispatch_routes_openai(monkeypatch):
    seen = {}
    monkeypatch.setattr(ai, "_call_openai", lambda p, m, mt=300: seen.setdefault("o", (p, m, mt)) or "x")
    ai.set_backend("openai")
    ai.dispatch("hi", mode="local", max_tokens=250)
    assert seen["o"][1] == "gpt-4o" and seen["o"][2] == 250


def test_dispatch_routes_gemini(monkeypatch):
    seen = {}
    monkeypatch.setattr(ai, "_call_gemini", lambda p, m, mt=300: seen.setdefault("g", (p, m, mt)) or "x")
    ai.dispatch("hi", mode="cloud", backend="gemini")
    assert seen["g"][1] == "gemini-1.5-flash"


def test_dispatch_uses_backend_default_when_wrong_default_leaks(monkeypatch):
    # doc/insights default model to 'llama3.1:8b' from mode; if the backend is
    # openai, dispatch should swap in the openai default instead.
    seen = {}
    monkeypatch.setattr(ai, "_call_openai", lambda p, m, mt=300: seen.setdefault("o", m) or "x")
    ai.set_backend("openai")
    ai.dispatch("hi", mode="local", model="llama3.1:8b")
    assert seen["o"] == "gpt-4o"


def test_dispatch_respects_explicit_custom_model(monkeypatch):
    seen = {}
    monkeypatch.setattr(ai, "_call_openai", lambda p, m, mt=300: seen.setdefault("o", m) or "x")
    ai.set_backend("openai")
    ai.dispatch("hi", model="gpt-4-turbo")
    assert seen["o"] == "gpt-4-turbo"


# --- optional-dependency errors are actionable ------------------------------

def test_openai_missing_dep_message(monkeypatch):
    ai._openai_client = None
    monkeypatch.setitem(sys.modules, "openai", None)  # force ImportError
    with pytest.raises(ImportError) as e:
        ai._get_openai_client()
    assert "pip install sqldoc[openai]" in str(e.value)


def test_gemini_missing_dep_message(monkeypatch):
    monkeypatch.setitem(sys.modules, "google.generativeai", None)
    with pytest.raises(ImportError) as e:
        ai._call_gemini("hi")
    assert "pip install sqldoc[gemini]" in str(e.value)


# --- backends actually call the SDKs when present ---------------------------

def test_openai_backend_calls_sdk(monkeypatch):
    ai._openai_client = None
    calls = {}

    class _Msg:
        content = "  desc  "

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    class _Client:
        class chat:
            class completions:
                @staticmethod
                def create(model, max_tokens, messages):
                    calls.update(model=model, max_tokens=max_tokens)
                    return _Resp()

    fake = types.SimpleNamespace(OpenAI=lambda: _Client())
    monkeypatch.setitem(sys.modules, "openai", fake)
    out = ai._call_openai("prompt", "gpt-4o", 123)
    assert out == "desc" and calls["model"] == "gpt-4o" and calls["max_tokens"] == 123
    ai._openai_client = None


def test_gemini_backend_calls_sdk(monkeypatch):
    ai._gemini_configured = False
    calls = {}

    class _Resp:
        text = " answer "

    class _Model:
        def __init__(self, model):
            calls["model"] = model

        def generate_content(self, prompt, generation_config=None):
            calls["cfg"] = generation_config
            return _Resp()

    fake = types.SimpleNamespace(
        configure=lambda api_key=None: calls.update(configured=True),
        GenerativeModel=_Model)
    google_mod = types.ModuleType("google")
    google_mod.generativeai = fake
    monkeypatch.setitem(sys.modules, "google", google_mod)
    monkeypatch.setitem(sys.modules, "google.generativeai", fake)
    out = ai._call_gemini("prompt", "gemini-1.5-flash", 77)
    assert out == "answer" and calls["cfg"]["max_output_tokens"] == 77
    ai._gemini_configured = False
