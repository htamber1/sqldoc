"""Live validation of the `sqldoc access` identity providers.

The directory back-ends (generic LDAP, Microsoft Graph / hybrid AD, Okta, Google
Workspace, JumpCloud) are mock-tested. This resolves a **real** user against the
configured directory so a developer can confirm the provider wiring; the SQL side
of the access suite is already live-validated in `tests/integration/test_access.py`.

Config: fill in the ``access.ad`` section of the live config (its ``type`` selects
the provider) and set ``SQLDOC_TEST_AD_USER`` to a real user identifier
(sAMAccountName / UPN / email, per your provider). Optionally set
``access.servers`` too, to run the full ``access check`` end-to-end.
"""
import os

import pytest

from _liveutil import live_config, live_config_path, run

pytestmark = pytest.mark.live


def _ad_config():
    access = live_config().get("access") or {}
    return access.get("ad") or {}


def _test_user():
    return os.environ.get("SQLDOC_TEST_AD_USER")


def test_directory_resolves_a_real_user():
    ad = _ad_config()
    if not ad or (ad.get("type") in (None, "native", "auto")):
        pytest.skip(f"set access.ad (type = ldap|generic-ldap|graph|hybrid|okta|"
                    f"google|jumpcloud) in {live_config_path()} to test a directory")
    user = _test_user()
    if not user:
        pytest.skip("set SQLDOC_TEST_AD_USER to a real user identifier for this directory")
    from sqldoc.access import ad as ad_mod
    source = ad_mod.get_source(ad)
    result = source.get_user(user)
    assert result.found, f"user {user!r} not found in {result.source or ad.get('type')}"
    print(f"\n[access.ad:{result.source}] resolved {user!r} -> "
          f"{result.display_name!r}, {len(result.groups)} group(s)")


def test_access_check_end_to_end():
    """Full check across the configured directory + SQL servers (needs both
    access.ad and access.servers, plus a reachable SQL Server)."""
    cfg = live_config()
    access = cfg.get("access") or {}
    if not (access.get("ad") and access.get("servers")):
        pytest.skip("set both access.ad and access.servers in the live config for the "
                    "end-to-end access check")
    user = _test_user()
    if not user:
        pytest.skip("set SQLDOC_TEST_AD_USER to run the end-to-end access check")
    r = run(["access", "check", "--user", user])
    assert r.exit_code == 0, f"access check failed:\n{r.output}"
    print(f"\n[access check] completed for {user!r} across the configured estate")
