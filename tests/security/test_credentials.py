"""Security: credentials are redacted from the audit log and never surface."""
from sqldoc import audit


def test_redact_hides_named_secrets():
    out = audit.redact_options({
        "server": "h", "database": "DB",
        "password": "hunter2", "connection_string": "DRIVER=x;PWD=s",
        "api_key": "abc", "bind_password": "x", "client_secret": "y",
        "access_token": "z", "webhook_url": "https://hook", "private_key": "k",
    })
    assert out["server"] == "h" and out["database"] == "DB"
    for k in ("password", "connection_string", "api_key", "bind_password",
              "client_secret", "access_token", "webhook_url", "private_key"):
        assert out[k] == "***redacted***", k


def test_redact_hides_secret_by_value_under_benign_key():
    # Even under an innocuous key name, a value that embeds a credential is hidden.
    out = audit.redact_options({
        "note": "conn is postgresql://user:s3cret@db/x",
        "extra": "SERVER=h;PWD=leaked;UID=u",
        "plain": "just a normal note",
    })
    assert out["note"] == "***redacted***"
    assert out["extra"] == "***redacted***"
    assert out["plain"] == "just a normal note"


def test_redact_records_that_a_secret_was_used_not_its_value():
    out = audit.redact_options({"password": "hunter2"})
    assert out["password"] == "***redacted***"
    assert "hunter2" not in str(out)


def test_warn_if_insecure_permissions_is_safe_on_all_platforms(tmp_path):
    from sqldoc.validation import warn_if_insecure_permissions
    p = tmp_path / ".sqldoc.yml"
    p.write_text("password: x\n", encoding="utf-8")
    msgs = []
    # Never raises; on POSIX with a loose mode it warns, on Windows it's a no-op.
    warn_if_insecure_permissions(str(p), emit=msgs.append)
    assert isinstance(msgs, list)
