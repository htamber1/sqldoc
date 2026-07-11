"""AI retry/backoff and description-cache behavior (no live LLM)."""
import pytest

import sqldoc.ai as ai
from conftest import build_tables, build_views, build_procs


def test_retry_succeeds_after_transient_failures(monkeypatch):
    monkeypatch.setattr(ai.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("transient")
        return "ok"

    assert ai._retry(flaky, "unit") == "ok"
    assert calls["n"] == 3


def test_retry_raises_after_max_attempts(monkeypatch):
    monkeypatch.setattr(ai.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def always_fail():
        calls["n"] += 1
        raise TimeoutError("nope")

    with pytest.raises(TimeoutError):
        ai._retry(always_fail, "unit")
    assert calls["n"] == ai.MAX_ATTEMPTS


def test_cache_roundtrip(tmp_path):
    path = str(tmp_path / "sub" / "cache.json")
    ai.save_cache({"version": 1, "entries": {"k": "v"}}, path)
    loaded = ai.load_cache(path)
    assert loaded["entries"]["k"] == "v"


def test_load_cache_missing_returns_empty(tmp_path):
    loaded = ai.load_cache(str(tmp_path / "absent.json"))
    assert loaded["entries"] == {}


def _counting_call(counter):
    def fake(prompt, model):
        counter["n"] += 1
        return f"generated-{counter['n']}"
    return fake


def test_enrich_uses_cache(monkeypatch):
    counter = {"n": 0}
    monkeypatch.setattr(ai, "_call_ollama", _counting_call(counter))
    cache = {"version": 1, "entries": {}}

    t1 = build_tables()
    ai.enrich_tables(t1, mode="local", concurrency=1, cache=cache)
    cold = counter["n"]
    # 2 table descriptions + 4 undocumented columns (Orders.Id is pre-documented;
    # Orders also has CustomerID/LineTotal/Status, Archive has Id)
    assert cold == 6
    assert all(c.description for t in t1 for c in t.columns)

    # Second run over fresh objects with the same cache: everything reused.
    t2 = build_tables()
    ai.enrich_tables(t2, mode="local", concurrency=1, cache=cache)
    assert counter["n"] == cold
    assert t2[0].description and t2[0].columns[1].description


def test_cache_miss_on_structure_change(monkeypatch):
    counter = {"n": 0}
    monkeypatch.setattr(ai, "_call_ollama", _counting_call(counter))
    cache = {"version": 1, "entries": {}}

    ai.enrich_tables(build_tables(), mode="local", concurrency=1, cache=cache)
    before = counter["n"]

    changed = build_tables()
    changed[0].columns[1].data_type = "bigint"   # alters table + column signatures
    ai.enrich_tables(changed, mode="local", concurrency=1, cache=cache)
    assert counter["n"] > before


def test_include_definitions_adds_body_to_prompts(monkeypatch):
    captured = {}
    monkeypatch.setattr(ai, "_call_ollama", lambda prompt, model: captured.setdefault("p", prompt) or "d")

    captured.clear()
    ai.generate_view_description(build_views()[0], mode="local", include_definitions=True)
    assert "SQL definition:" in captured["p"] and "CREATE VIEW" in captured["p"]

    captured.clear()
    ai.generate_view_description(build_views()[0], mode="local", include_definitions=False)
    assert "SQL definition:" not in captured["p"]

    captured.clear()
    ai.generate_procedure_description(build_procs()[0], mode="local", include_definitions=True)
    assert "SQL definition:" in captured["p"] and "CREATE PROCEDURE" in captured["p"]

    captured.clear()
    ai.generate_table_description(build_tables()[0], mode="local", include_definitions=True)
    assert "Trigger definitions:" in captured["p"] and "CREATE TRIGGER" in captured["p"]


def test_include_definitions_changes_cache_signature():
    v = build_views()[0]
    s_meta = ai._sig_view(v, include_definitions=False)
    s_def = ai._sig_view(v, include_definitions=True)
    assert s_meta != s_def                                  # def folded into sig
    v.definition = "CREATE VIEW x AS SELECT 2 AS Two;"
    assert ai._sig_view(v, include_definitions=True) != s_def   # body change invalidates
    assert ai._sig_view(v, include_definitions=False) == s_meta  # metadata sig unaffected


def test_no_cache_always_generates(monkeypatch):
    counter = {"n": 0}
    monkeypatch.setattr(ai, "_call_ollama", _counting_call(counter))
    ai.enrich_tables(build_tables(), mode="local", concurrency=1, cache=None)
    first = counter["n"]
    ai.enrich_tables(build_tables(), mode="local", concurrency=1, cache=None)
    assert counter["n"] == first * 2
