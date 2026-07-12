"""Natural-language alert rules for the agent.

Users write plain-English alert rules in the ``alerts:`` section of
``.sqldoc.yml`` (e.g. "alert when any database has not been backed up in 24
hours", or "alert when a job fails three times in a row"). On each poll the
agent sends those rules together with a metadata-only snapshot of the current
database state (and recent event history) to the LLM, which decides — per rule —
whether it should fire and writes the notification message.

Only metrics + event headlines are sent to the model — never table row data.
"""
import json
import re

import sqldoc.ai as ai


def _ai_call(prompt, mode, model):
    return ai.dispatch(prompt, mode, model, max_tokens=700).strip()


def build_prompt(rules, context) -> str:
    numbered = "\n".join(f"{i+1}. {r}" for i, r in enumerate(rules))
    ctx = json.dumps(context, indent=2, default=str)
    return (
        "You are a database monitoring assistant. Below are alert rules and the "
        "current state of a monitored database (metrics + recent event history). "
        "For EACH rule, decide whether it should fire right now based ONLY on the "
        "provided state.\n\n"
        "Be CONSERVATIVE: fire a rule ONLY if the current state clearly and "
        "specifically satisfies its condition. If the relevant data is absent, "
        "zero, or does not clearly meet the threshold, do NOT fire that rule.\n\n"
        f"ALERT RULES:\n{numbered}\n\n"
        f"CURRENT STATE (JSON):\n{ctx}\n\n"
        "Respond with ONLY a JSON array, one object per rule that should FIRE "
        '(omit rules that should not fire): '
        '[{"rule": <rule number>, "message": "<short human-readable alert citing the specific value>"}]. '
        "If no rule should fire, respond with []. Do not add any text outside the JSON.")


def parse_decisions(raw: str, rules: list) -> list:
    """Extract fired alerts from the model's response. Returns
    [{'rule': <text>, 'message': <text>}], robust to extra prose around the JSON."""
    if not raw:
        return []
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        return []
    try:
        decisions = json.loads(m.group(0))
    except (ValueError, TypeError):
        return []
    fired = []
    for d in decisions:
        if not isinstance(d, dict):
            continue
        idx = d.get("rule")
        try:
            rule_text = rules[int(idx) - 1]
        except (TypeError, ValueError, IndexError):
            rule_text = str(idx)
        message = str(d.get("message") or rule_text).strip()
        fired.append({"rule": rule_text, "message": message})
    return fired


def evaluate(rules, context, mode="local", model=None) -> list:
    """Ask the LLM which rules fire. Returns the fired alerts (may be empty)."""
    if not rules:
        return []
    raw = _ai_call(build_prompt(rules, context), mode, model)
    return parse_decisions(raw, rules)
