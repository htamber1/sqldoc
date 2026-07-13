"""Security: SQL/ODBC injection guards, identifier quoting, path traversal."""
import pytest

from sqldoc.validation import (validate_server, validate_database,
                               validate_username, ValidationError)
from sqldoc.adapters.sqlserver import SqlServerAdapter


# --- ODBC connection-string injection --------------------------------------

@pytest.mark.parametrize("bad", [
    "host;Trusted_Connection=yes",
    "db}",
    "user{x",
    "h=1",
    "line\nbreak",
    "nul\x00byte",
])
def test_connection_parts_reject_odbc_separators(bad):
    for fn in (validate_server, validate_database, validate_username):
        with pytest.raises(ValidationError):
            fn(bad)


def test_connection_parts_accept_real_values():
    assert validate_server(r"HOST\SQLEXPRESS,1433")
    assert validate_database("My Data.Base")
    assert validate_username(r"DOMAIN\user")
    assert validate_username("user@contoso.com")


def test_build_connection_string_brace_quotes_password():
    # A password containing an ODBC separator must not inject a new attribute.
    cs = SqlServerAdapter.build_connection_string("h", "d", "u", "p;DROP=1")
    assert "PWD={p;DROP=1};" in cs           # whole password stays one value
    assert "DROP=1;" not in cs.replace("{p;DROP=1}", "")  # not a bare attribute


def test_build_connection_string_rejects_injected_server():
    with pytest.raises(ValidationError):
        SqlServerAdapter.build_connection_string(
            "h;UID=attacker", "d", "u", "p")


# --- identifier quoting (doubles the close-quote) --------------------------

def test_quality_profile_quotes_double_close_quote():
    from sqldoc.quality import _SQLSERVER, _POSTGRES, _MYSQL
    assert _SQLSERVER.quote("a]b") == "[a]]b]"
    assert _POSTGRES.quote('a"b') == '"a""b"'
    assert _MYSQL.quote("a`b") == "`a``b`"


def test_pii_quote_ident_escapes_bracket():
    from sqldoc.pii import _quote_ident
    assert _quote_ident("Col]; DROP") == "[Col]]; DROP]"


# --- generated-script literal escaping -------------------------------------

def test_access_script_escapes_single_quote_in_login():
    from sqldoc.access.script import _lit, _q
    assert _lit("o'brien") == "o''brien"
    assert _q("wei]rd") == "[wei]]rd]"


# --- filename / path traversal ---------------------------------------------

@pytest.mark.parametrize("name,expected_safe", [
    ("../../etc/passwd", "___etc_passwd"),
    ("..", "db"),
    ("...", "db"),
    ("", "db"),
    (r"a/b\c", "a_b_c"),
    (".hidden", "hidden"),
])
def test_safe_filename_neutralizes_traversal(name, expected_safe):
    from sqldoc.cli import _safe_filename
    out = _safe_filename(name)
    assert out == expected_safe
    assert "/" not in out and "\\" not in out and ".." not in out


def test_validate_output_path_blocks_traversal_and_nul(tmp_path):
    from sqldoc.validation import validate_output_path
    with pytest.raises(ValidationError):
        validate_output_path(str(tmp_path / ".." / "escape.html"), base_dir=str(tmp_path))
    with pytest.raises(ValidationError):
        validate_output_path("ok\x00.html")
    # A path inside the base dir is allowed.
    good = validate_output_path(str(tmp_path / "report.html"), base_dir=str(tmp_path))
    assert good.endswith("report.html")
