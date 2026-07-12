"""Natural-language alert rules: prompt, parsing, evaluation, poller, config."""
from click.testing import CliRunner

from sqldoc.agent import nl_alerts
from sqldoc.agent.config import parse_agent_config


# --- parsing ----------------------------------------------------------------

RULES = ["alert when a database has not been backed up in 24 hours",
         "alert when a job fails three times in a row"]


def test_parse_decisions_valid():
    raw = '[{"rule": 1, "message": "DB prod not backed up for 30h"}]'
    fired = nl_alerts.parse_decisions(raw, RULES)
    assert len(fired) == 1
    assert fired[0]["rule"] == RULES[0] and "30h" in fired[0]["message"]


def test_parse_decisions_with_surrounding_text():
    raw = 'Sure! Here is the result:\n[{"rule": 2, "message": "Job X failed 3x"}]\nDone.'
    fired = nl_alerts.parse_decisions(raw, RULES)
    assert fired[0]["rule"] == RULES[1]


def test_parse_decisions_empty_and_invalid():
    assert nl_alerts.parse_decisions("[]", RULES) == []
    assert nl_alerts.parse_decisions("no json here", RULES) == []
    assert nl_alerts.parse_decisions("[not valid json", RULES) == []


def test_build_prompt_includes_rules_and_context():
    prompt = nl_alerts.build_prompt(RULES, {"database": "prod", "health_issues": 5})
    assert "1. alert when a database" in prompt and "2. alert when a job" in prompt
    assert '"database": "prod"' in prompt and "JSON array" in prompt


def test_evaluate(monkeypatch):
    monkeypatch.setattr(nl_alerts, "_ai_call",
                        lambda p, m, mo: '[{"rule": 1, "message": "fire it"}]')
    fired = nl_alerts.evaluate(RULES, {"x": 1}, mode="local")
    assert fired == [{"rule": RULES[0], "message": "fire it"}]
    assert nl_alerts.evaluate([], {}, mode="local") == []      # no rules -> no AI call


# --- config -----------------------------------------------------------------

def test_config_parses_alerts():
    cfg = {"agent": {"databases": [{"name": "a", "connection_string": "x"}],
                     "alerts": ["alert when disk is nearly full",
                                "  ",   # blanks dropped
                                "alert on any HIGH pii"]}}
    ac = parse_agent_config(cfg)
    assert ac.nl_alerts == ["alert when disk is nearly full", "alert on any HIGH pii"]


def test_config_rejects_bad_alerts():
    import pytest
    with pytest.raises(ValueError):
        parse_agent_config({"agent": {"databases": [{"name": "a", "connection_string": "x"}],
                                      "alerts": [{"not": "a string"}]}})
