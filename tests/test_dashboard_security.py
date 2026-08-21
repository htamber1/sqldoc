"""Dashboard security: a GET must not change state.

THE DEFECT, demonstrated against a running agent: a single unauthenticated

    GET /access/approve?token=<token>

flipped a pending database-access approval to "approved". No confirmation, no
POST, and -- by default -- no authentication on the dashboard at all.

Those links are EMAILED to approvers. Mail-security gateways, link scanners,
URL-rewriting proxies and browser prefetchers all issue GETs against links in
mail with no human involved. Any of them silently recorded an approval.

The fix keeps the emailed link clickable but makes the GET inert: it renders a
confirmation page, and only the POST that page submits records the decision.

Everything here is offline: the handler is driven directly, no socket is bound.
"""
import json

import pytest

from sqldoc.agent import dashboard
from sqldoc.agent.store import AgentStore
from sqldoc.agent import db_path


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLDOC_AGENT_HOME", str(tmp_path / "home"))


@pytest.fixture
def pending(tmp_path):
    """A pending approval in an isolated approvals store."""
    from types import SimpleNamespace
    from sqldoc.access import approval
    script = SimpleNamespace(server="devbox", database="testdb",
                             login_name="DOMAIN\\svc_test", role="db_datareader",
                             grant_sql="-- grant", rollback_sql="-- rollback")
    rec = approval.submit_approval({}, script, requester="tester", send_email=False)
    return rec["token"]


def _status(token):
    from sqldoc.access import approval
    return approval.get_approval(token)["status"]


# ============================================================================
# the fix
# ============================================================================

def test_get_on_an_approval_link_records_nothing(pending):
    """THE REGRESSION TEST. A bare GET is what a link scanner issues."""
    page = dashboard.render_approval_confirm(f"/access/approve?token={pending}")
    assert _status(pending) == "pending", (
        "a bare GET recorded the decision; an emailed link fetched by a mail "
        "scanner or prefetcher would silently approve database access")
    assert "Confirm" in page


def test_get_on_a_reject_link_records_nothing(pending):
    dashboard.render_approval_confirm(f"/access/reject?token={pending}")
    assert _status(pending) == "pending"


def test_the_confirmation_page_posts_back(pending):
    """The decision must be reachable only through a form submission."""
    page = dashboard.render_approval_confirm(f"/access/approve?token={pending}")
    assert "method='POST'" in page or 'method="POST"' in page
    assert "action='/access/approve'" in page or 'action="/access/approve"' in page
    assert pending in page, "the token must be carried into the form"


def test_post_records_the_decision(pending):
    dashboard.handle_approval_decision("/access/approve", {"token": [pending]})
    assert _status(pending) == "approved"


def test_post_records_a_rejection_with_a_reason(pending):
    dashboard.handle_approval_decision(
        "/access/reject", {"token": [pending], "reason": ["not needed"]})
    from sqldoc.access import approval
    rec = approval.get_approval(pending)
    assert rec["status"] == "rejected"
    assert rec["reason"] == "not needed"


def test_confirmation_page_shows_what_is_being_approved(pending):
    """An approver must see the request before confirming it."""
    page = dashboard.render_approval_confirm(f"/access/approve?token={pending}")
    assert "testdb" in page
    assert "db_datareader" in page
    assert "tester" in page


def test_an_already_decided_request_is_not_re_offered(pending):
    dashboard.handle_approval_decision("/access/approve", {"token": [pending]})
    page = dashboard.render_approval_confirm(f"/access/approve?token={pending}")
    assert "Already" in page
    assert "method='POST'" not in page, "a decided request must not offer the form again"


def test_unknown_token_is_reported_without_a_form(pending):
    page = dashboard.render_approval_confirm("/access/approve?token=nosuchtoken")
    assert "No pending approval" in page
    assert "method='POST'" not in page


def test_missing_token_is_handled(pending):
    page = dashboard.render_approval_confirm("/access/approve")
    assert "No pending approval" in page


def test_post_with_an_unknown_token_is_an_error_page_not_a_crash():
    page = dashboard.handle_approval_decision("/access/approve", {"token": ["nope"]})
    assert "No pending approval" in page


def test_token_is_escaped_in_the_error_page():
    """The token is attacker-controlled input reflected into HTML."""
    page = dashboard.render_approval_confirm(
        "/access/approve?token=%3Cscript%3Ealert(1)%3C/script%3E")
    assert "<script>alert(1)</script>" not in page


def test_reason_is_escaped_into_the_form(pending):
    page = dashboard.render_approval_confirm(
        f"/access/approve?token={pending}&reason=%22%3E%3Cscript%3E")
    assert "><script>" not in page


# ============================================================================
# the surface that must not regress
# ============================================================================

def test_bind_host_defaults_to_loopback():
    """Binding anything else would publish schema/PII/health to the network."""
    import inspect
    sig = inspect.signature(dashboard.make_server)
    assert sig.parameters["host"].default == "127.0.0.1"

    from sqldoc.agent.daemon import run_daemon
    assert inspect.signature(run_daemon).parameters["host"].default == "127.0.0.1"


def test_security_headers_block_external_content():
    """The pages can show PII counts, so the CSP must not allow external loading."""
    csp = dashboard._DASH_SECURITY_HEADERS["Content-Security-Policy"]
    assert "default-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert dashboard._DASH_SECURITY_HEADERS["X-Frame-Options"] == "DENY"
    assert dashboard._DASH_SECURITY_HEADERS["Cache-Control"] == "no-store"


def test_pages_reference_no_external_resources():
    """Air-gap: a page that can display monitoring data must be self-contained."""
    import re
    store = AgentStore(db_path())
    external = re.compile(r"""(?:src|href)\s*=\s*['"]?(?:https?:)?//""", re.I)
    for page in (dashboard.render_overview(store),
                 dashboard.render_alerts(store),
                 dashboard.render_db_page(store, "nope")):
        assert not external.search(page)


def test_overview_json_exposes_no_connection_details():
    """The JSON is served unauthenticated; it must not carry credentials."""
    store = AgentStore(db_path())
    store.start_run("db1")
    payload = json.dumps(dashboard.overview_json(store)).lower()
    for secret in ("password", "connection_string", "pwd=", "trusted_connection",
                   "driver=", "token"):
        assert secret not in payload, f"overview JSON leaks {secret!r}"
