"""Escalation flags and server-scoped grants must reach the rendered reports.

`collect_db_access` records escalation routes on `DatabaseAccess.flags` instead of
folding them into `level` (so the effective access is not overstated), and
`collect_server_logins` records server-scoped grants on
`Login.server_permissions` (CONTROL SERVER / ALTER ANY LOGIN / a resolved
IMPERSONATE are permissions, not role memberships).

Both were collected and exported nowhere, so the risk signal was invisible in
the HTML and absent from the JSON. A collected-but-never-displayed risk signal
is useless -- these tests pin that it is displayed.

The pinned JSON key sets live in tests/regression/test_contracts.py.
"""
import pytest

from sqldoc.access.model import AccessReport, ADUser, DatabaseAccess, Login
from sqldoc.access.render import build_check_json, render_check_html
from sqldoc.offline import verify_file

TAKE_OWNERSHIP_FLAG = ("TAKE OWNERSHIP: can seize ownership of the securable "
                       "(escalation route)")


def report_with(flags=(), server_roles=(), server_permissions=(), level="read"):
    report = AccessReport(user=ADUser(identifier="DOM\\jsmith",
                                      display_name="J Smith", found=True,
                                      source="ldap"))
    report.logins.append(Login(name="DOM\\D-Group", type="WINDOWS_GROUP",
                               server_roles=list(server_roles),
                               server_permissions=list(server_permissions)))
    report.access.append(DatabaseAccess(
        server="srv", database="AppDB", login="DOM\\D-Group",
        db_user="DOM\\D-Group", via="group DOM\\D-Group",
        roles=["db_datareader"], level=level, flags=list(flags)))
    return report


def render(tmp_path, report):
    out = tmp_path / "check.html"
    render_check_html(report, str(out))
    return out.read_text(encoding="utf-8")


# --- flags in the HTML ------------------------------------------------------

@pytest.fixture
def flagged_html(tmp_path):
    return render(tmp_path, report_with(flags=[TAKE_OWNERSHIP_FLAG]))


def test_flagged_html_has_an_escalation_section(flagged_html):
    assert "Escalation routes flagged" in flagged_html


def test_flagged_html_shows_the_route_text(flagged_html):
    assert "TAKE OWNERSHIP" in flagged_html
    assert "escalation route" in flagged_html


def test_flagged_html_attributes_the_route_to_its_login_and_database(flagged_html):
    assert "D-Group" in flagged_html
    assert "AppDB" in flagged_html


def test_flagged_html_states_the_level_is_not_raised(flagged_html):
    """The reader must not read a flag as an admin finding."""
    assert "do not raise the effective level" in flagged_html


def test_unflagged_html_has_no_escalation_section(tmp_path):
    assert "Escalation routes flagged" not in render(tmp_path, report_with())


def test_flags_do_not_change_the_reported_level(tmp_path):
    """The level pill still reflects the real level, not the escalation route."""
    html = render(tmp_path, report_with(flags=[TAKE_OWNERSHIP_FLAG], level="read"))
    assert "<span class='pill read'>read</span>" in html


# --- server-scoped grants in the HTML ---------------------------------------

@pytest.fixture
def privileged_html(tmp_path):
    return render(tmp_path, report_with(
        server_roles=["securityadmin"],
        server_permissions=["CONTROL SERVER", "IMPERSONATE ON LOGIN::sa"]))


def test_server_privileges_section_is_rendered(privileged_html):
    assert "Server-level privileges" in privileged_html


def test_server_scoped_grants_are_shown(privileged_html):
    """These never appear in sys.server_role_members, so `server_roles` alone
    would hide them entirely."""
    assert "CONTROL SERVER" in privileged_html
    assert "IMPERSONATE ON LOGIN::sa" in privileged_html


def test_server_roles_are_shown_alongside(privileged_html):
    assert "securityadmin" in privileged_html


def test_server_privileges_section_explains_the_reach(privileged_html):
    assert "reaches every database" in privileged_html


def test_no_server_privileges_section_without_any(tmp_path):
    assert "Server-level privileges" not in render(tmp_path, report_with())


def test_server_privileges_render_without_a_database_principal(tmp_path):
    """The sysadmin case: a group login with server-wide privilege and no
    database access row at all must still surface its privileges."""
    report = AccessReport(user=ADUser(identifier="DOM\\jsmith", found=True))
    report.logins.append(Login(name="DOM\\D-Admins", type="WINDOWS_GROUP",
                               server_roles=["sysadmin"]))
    html = render(tmp_path, report)
    assert "Server-level privileges" in html
    assert "sysadmin" in html


# --- the HTML stays safe ----------------------------------------------------

def test_report_with_new_sections_is_air_gap_safe(tmp_path):
    out = tmp_path / "check.html"
    render_check_html(report_with(flags=[TAKE_OWNERSHIP_FLAG],
                                  server_roles=["securityadmin"],
                                  server_permissions=["CONTROL SERVER"]), str(out))
    assert verify_file(str(out)) == []


def test_new_sections_escape_their_values(tmp_path):
    """Login names come from the server; they are escaped like everything else."""
    report = report_with(flags=["<script>alert(1)</script>"],
                         server_permissions=["<img src=x>"])
    report.access[0].login = "DOM\\<evil>"
    html = render(tmp_path, report)
    assert "<script>alert(1)</script>" not in html
    assert "<img src=x>" not in html
    assert "&lt;script&gt;" in html


# --- flags + server_permissions in the JSON ---------------------------------

def test_flags_are_exported_in_the_json():
    j = build_check_json(report_with(flags=[TAKE_OWNERSHIP_FLAG]))
    assert j["access"][0]["flags"] == [TAKE_OWNERSHIP_FLAG]


def test_flags_default_to_an_empty_list_in_the_json():
    assert build_check_json(report_with())["access"][0]["flags"] == []


def test_server_permissions_are_exported_in_the_json():
    j = build_check_json(report_with(server_permissions=["CONTROL SERVER"]))
    assert j["logins"][0]["server_permissions"] == ["CONTROL SERVER"]


def test_server_permissions_default_to_an_empty_list_in_the_json():
    assert build_check_json(report_with())["logins"][0]["server_permissions"] == []


def test_the_json_stays_serializable():
    import json
    payload = build_check_json(report_with(flags=[TAKE_OWNERSHIP_FLAG],
                                           server_permissions=["CONTROL SERVER"]))
    assert "CONTROL SERVER" in json.dumps(payload)
