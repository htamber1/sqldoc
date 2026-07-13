"""Live validation of the AI backends.

sqldoc funnels every AI feature through ``sqldoc.ai.dispatch(prompt, mode,
model, backend)``. Ollama (local) and Anthropic (cloud) are already
live-validated; OpenAI and Gemini are mock-only. This calls each backend for
real when its key/endpoint is available, so a developer can confirm the wiring
against the live provider.

Gates:
  OpenAI     OPENAI_API_KEY
  Gemini     GOOGLE_API_KEY
  Anthropic  ANTHROPIC_API_KEY
  Ollama     SQLDOC_TEST_OLLAMA=1  (and a local Ollama at :11434 with a model)
"""
import pytest

from _liveutil import requires_env, env

pytestmark = pytest.mark.live

_PROMPT = ("Reply with exactly the single word: OK. "
           "This is a connectivity test for a database documentation tool.")


def _dispatch(backend, mode="cloud"):
    from sqldoc import ai
    return ai.dispatch(_PROMPT, mode=mode, backend=backend, max_tokens=20)


@requires_env("OPENAI_API_KEY")
def test_openai_backend():
    out = _dispatch("openai")
    assert out and isinstance(out, str), "OpenAI returned nothing"
    print(f"\n[openai] live response: {out.strip()[:80]!r}")


@requires_env("GOOGLE_API_KEY")
def test_gemini_backend():
    out = _dispatch("gemini")
    assert out and isinstance(out, str), "Gemini returned nothing"
    print(f"\n[gemini] live response: {out.strip()[:80]!r}")


@requires_env("ANTHROPIC_API_KEY")
def test_anthropic_backend():
    out = _dispatch("anthropic")
    assert out and isinstance(out, str), "Anthropic returned nothing"
    print(f"\n[anthropic] live response: {out.strip()[:80]!r}")


@pytest.mark.skipif(not env("SQLDOC_TEST_OLLAMA"),
                    reason="set SQLDOC_TEST_OLLAMA=1 with a local Ollama running")
def test_ollama_backend():
    out = _dispatch("ollama", mode="local")
    assert out and isinstance(out, str), "Ollama returned nothing"
    print(f"\n[ollama] live response: {out.strip()[:80]!r}")
