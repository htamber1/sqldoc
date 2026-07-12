"""Cover pii.py sampling / AI-confirmation / confidence edge paths."""
import pytest

from sqldoc import pii
from sqldoc.pii import Finding, _quote_ident, filter_by_confidence, _ai_confirm, _sample_values


def _f(category="email", risk="MEDIUM", score=0.7):
    return Finding("Sales", "Customer", "Email", "varchar", category, risk,
                   "name", ["GDPR"], "Encrypt", confidence_score=score)


# --- small helpers ---------------------------------------------------------

def test_quote_ident_escapes():
    assert _quote_ident("weird]col") == "[weird]]col]"


def test_filter_by_confidence():
    findings = [_f(score=0.9), _f(score=0.3)]
    kept, dropped = filter_by_confidence(findings, 0.5)
    assert len(kept) == 1 and dropped == 1
    kept2, dropped2 = filter_by_confidence(findings, 0.0)
    assert dropped2 == 0


# --- sampling --------------------------------------------------------------

class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.sql = None

    def execute(self, sql):
        self.sql = sql

    def fetchall(self):
        return self.rows


def test_sample_values_filters_none():
    cur = FakeCursor([("a@x.com",), (None,), ("b@x.com",)])
    vals = _sample_values(cur, "Sales", "Customer", "Email")
    assert vals == ["a@x.com", "b@x.com"]
    assert "TOP 5" in cur.sql and "[Email]" in cur.sql


# --- AI confirm ------------------------------------------------------------

@pytest.mark.parametrize("reply,expected", [
    ("YES, definitely", "YES"),
    ("No these are ids", "NO"),
    ("hard to say", "UNSURE"),
])
def test_ai_confirm(monkeypatch, reply, expected):
    monkeypatch.setattr(pii.ai, "dispatch", lambda *a, **k: reply)
    assert _ai_confirm("email", ["a@x.com"], "local", None) == expected


# --- confirm_with_sampling -------------------------------------------------

class FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return FakeCursor(self._rows)

    def close(self):
        pass


def test_confirm_with_sampling_yes(monkeypatch):
    monkeypatch.setattr(pii, "get_connection", lambda cs: FakeConn([("a@x.com",)]))
    monkeypatch.setattr(pii.ai, "dispatch", lambda *a, **k: "YES")
    findings = [_f()]
    out = pii.confirm_with_sampling(findings, "cs", "local", None)
    assert out[0].confidence_score == 0.97 and "AI-confirmed" in out[0].confidence


def test_confirm_with_sampling_no_downgrades(monkeypatch):
    monkeypatch.setattr(pii, "get_connection", lambda cs: FakeConn([("12345",)]))
    monkeypatch.setattr(pii.ai, "dispatch", lambda *a, **k: "NO")
    findings = [_f(risk="HIGH")]
    out = pii.confirm_with_sampling(findings, "cs", "local", None)
    assert out[0].confidence_score == 0.1 and out[0].risk == "LOW"


def test_confirm_with_sampling_no_data(monkeypatch):
    monkeypatch.setattr(pii, "get_connection", lambda cs: FakeConn([]))     # no rows
    called = []
    monkeypatch.setattr(pii.ai, "dispatch", lambda *a, **k: called.append(1) or "YES")
    findings = [_f()]
    out = pii.confirm_with_sampling(findings, "cs", "local", None)
    assert "no data to sample" in out[0].confidence and not called    # AI never called


def test_confirm_with_sampling_progress(monkeypatch):
    monkeypatch.setattr(pii, "get_connection", lambda cs: FakeConn([("a@x.com",)]))
    monkeypatch.setattr(pii.ai, "dispatch", lambda *a, **k: "UNSURE")
    seen = []
    pii.confirm_with_sampling([_f(score=0.9)], "cs", "local", None,
                              progress=lambda i, n, f: seen.append((i, n)))
    assert seen == [(1, 1)]
