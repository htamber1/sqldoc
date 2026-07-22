"""Central input-validation layer.

A single place for validating externally-supplied values — CLI arguments,
`.sqldoc.yml` config values, and REST API parameters — so validation is
consistent and testable instead of ad-hoc per call site. Every validator either
returns a normalized value or raises :class:`ValidationError`; nothing here has
side effects.

Design goals:
  * reject the characters that let a value *break out of its context* — ODBC
    connection-string separators (``;`` ``{`` ``}`` ``=`` newlines), path
    traversal, non-allowlisted URL schemes;
  * be permissive about legitimate values (named SQL Server instances,
    host,port syntax, Azure FQDNs, quoted identifiers) so real deployments are
    not blocked;
  * carry a clear message naming the offending field.
"""
from __future__ import annotations

import ipaddress
import os
import re
from urllib.parse import urlparse


class ValidationError(ValueError):
    """Raised when an external input fails validation. Carries a human-readable
    message that is safe to show the user (names the field, not internals)."""


# Characters that must never appear in a value interpolated into an ODBC
# connection string (they would start a new attribute or a brace-quoted value).
_ODBC_FORBIDDEN = set(";{}=\r\n\x00")

# Control characters (except tab) are rejected everywhere.
_CONTROL = {chr(c) for c in range(0x20) if c != 0x09} | {"\x7f"}


def _no_control(value: str, field: str) -> None:
    bad = _CONTROL.intersection(value)
    if bad:
        raise ValidationError(
            f"{field} contains control characters "
            f"(0x{ord(next(iter(bad))):02x}); rejected.")


def validate_server(server: str, field: str = "server") -> str:
    """Validate a database host/server value.

    Accepts hostnames, IPv4/IPv6 literals, Azure FQDNs, SQL Server named
    instances (``host\\instance``) and ``host,port`` / ``tcp:host`` forms.
    Rejects ODBC separators and control characters (connection-string
    injection) and anything over 255 chars.
    """
    if server is None or str(server).strip() == "":
        raise ValidationError(f"{field} must not be empty.")
    server = str(server).strip()
    if len(server) > 255:
        raise ValidationError(f"{field} is too long (>255 chars).")
    _no_control(server, field)
    bad = _ODBC_FORBIDDEN.intersection(server)
    if bad:
        raise ValidationError(
            f"{field} contains a forbidden character ({''.join(sorted(bad))!r}); "
            "this could inject connection-string attributes.")
    return server


def validate_database(database: str, field: str = "database") -> str:
    """Validate a database/catalog name. Rejects ODBC separators, control
    characters, and over-length values; otherwise permissive (real DB names can
    contain spaces, dots, and Unicode)."""
    if database is None or str(database).strip() == "":
        raise ValidationError(f"{field} must not be empty.")
    database = str(database).strip()
    if len(database) > 128:
        raise ValidationError(f"{field} is too long (>128 chars).")
    _no_control(database, field)
    bad = _ODBC_FORBIDDEN.intersection(database)
    if bad:
        raise ValidationError(
            f"{field} contains a forbidden character ({''.join(sorted(bad))!r}).")
    return database


def validate_username(username: str, field: str = "username") -> str:
    """Validate a login/username. Allows SQL logins, ``DOMAIN\\user`` and
    ``user@domain`` (Azure AD) forms; rejects ODBC separators + control chars."""
    if username is None or str(username).strip() == "":
        raise ValidationError(f"{field} must not be empty.")
    username = str(username).strip()
    if len(username) > 256:
        raise ValidationError(f"{field} is too long (>256 chars).")
    _no_control(username, field)
    bad = _ODBC_FORBIDDEN.intersection(username)
    if bad:
        raise ValidationError(
            f"{field} contains a forbidden character ({''.join(sorted(bad))!r}).")
    return username


def validate_driver(driver: str, field: str = "driver") -> str:
    """Validate an ODBC driver name (from ``.sqldoc.yml`` ``driver:`` or a CLI
    flag). The value is brace-quoted into ``DRIVER={...}`` by the connection
    builder, so a literal ``}`` (or a control char) could break out of the
    braces; reject those. Real driver names are plain text like
    ``ODBC Driver 17 for SQL Server`` / ``SQL Server Native Client 11.0``."""
    if driver is None or str(driver).strip() == "":
        raise ValidationError(f"{field} must not be empty.")
    driver = str(driver).strip()
    if len(driver) > 128:
        raise ValidationError(f"{field} is too long (>128 chars).")
    _no_control(driver, field)
    bad = {"{", "}"}.intersection(driver)
    if bad:
        raise ValidationError(
            f"{field} contains a forbidden character ({''.join(sorted(bad))!r}); "
            "this could inject connection-string attributes.")
    return driver


def validate_port(port, field: str = "port") -> int:
    """Validate a TCP port number (1-65535)."""
    try:
        p = int(port)
    except (TypeError, ValueError):
        raise ValidationError(f"{field} must be an integer, got {port!r}.")
    if not (1 <= p <= 65535):
        raise ValidationError(f"{field} must be between 1 and 65535, got {p}.")
    return p


# --- file paths ------------------------------------------------------------

def validate_output_path(path: str, field: str = "output",
                         base_dir: str | None = None) -> str:
    """Validate an output file path and return its normalized absolute form.

    Rejects NUL bytes. When ``base_dir`` is given, the resolved path must stay
    inside it (path-traversal guard) — used where a caller/config controls the
    filename and it must not escape a working directory.
    """
    if path is None or str(path).strip() == "":
        raise ValidationError(f"{field} path must not be empty.")
    path = str(path)
    if "\x00" in path:
        raise ValidationError(f"{field} path contains a NUL byte.")
    resolved = os.path.realpath(os.path.abspath(path))
    if base_dir is not None:
        base = os.path.realpath(os.path.abspath(base_dir))
        if os.path.commonpath([resolved, base]) != base:
            raise ValidationError(
                f"{field} path escapes the allowed directory "
                f"({base}); refusing to write outside it.")
    return resolved


# --- URLs ------------------------------------------------------------------

def validate_url(url: str, field: str = "url",
                 allow_schemes=("https", "http")) -> str:
    """Validate an outbound URL: require an allowlisted scheme and a host.

    SSRF note: this validates *shape* only. Callers that must not reach internal
    addresses should additionally use :func:`is_internal_host` / pass a
    redirect-blocking transport — see ``sqldoc.nethttp``.
    """
    if url is None or str(url).strip() == "":
        raise ValidationError(f"{field} must not be empty.")
    url = str(url).strip()
    try:
        parsed = urlparse(url)
    except ValueError as e:
        raise ValidationError(f"{field} is not a valid URL: {e}.")
    if parsed.scheme.lower() not in allow_schemes:
        raise ValidationError(
            f"{field} scheme {parsed.scheme!r} not allowed "
            f"(permitted: {', '.join(allow_schemes)}).")
    if not parsed.hostname:
        raise ValidationError(f"{field} has no host.")
    return url


def warn_if_insecure_permissions(path: str, emit=None) -> bool:
    """Warn (best-effort) if a credential-bearing file is group/other-readable.

    POSIX only — the permission bits are meaningful there. On Windows, file
    access is governed by NTFS ACLs (not the mode bits), so this is a no-op and
    users should rely on ACL inheritance / EFS. Returns True if a warning was
    emitted. ``emit`` defaults to printing to stderr.
    """
    if os.name != "posix":
        return False
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return False
    group_other = mode & 0o077
    if not group_other:
        return False
    if emit is None:
        import sys
        def emit(msg):
            print(msg, file=sys.stderr)
    emit(f"Warning: {path} is readable by group/other (mode {oct(mode & 0o777)}); "
         f"it may contain credentials. Run: chmod 600 {path}")
    return True


def is_internal_host(host: str) -> bool:
    """Best-effort: True if ``host`` is loopback / link-local / private / a
    reserved literal, i.e. an SSRF target that outbound integrations should not
    reach. Hostnames that don't parse as IP literals return False here (they are
    resolved-and-checked at connect time by the SSRF-aware transport)."""
    if not host:
        return True
    h = host.strip().strip("[]").lower()
    if h in ("localhost", "localhost.localdomain"):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return (ip.is_loopback or ip.is_link_local or ip.is_private
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


# Cloud metadata endpoints — never a legitimate integration target.
_METADATA_HOSTS = {"169.254.169.254", "metadata.google.internal", "100.100.100.200"}


def assert_safe_outbound_url(url: str, field: str = "url",
                             allow_schemes=("https", "http")) -> str:
    """Validate an outbound URL *and* reject obvious SSRF targets (loopback,
    private ranges, cloud-metadata hosts) when the host is an IP literal or a
    known metadata name. For hostnames, DNS-rebinding-safe enforcement happens
    in the transport layer."""
    url = validate_url(url, field=field, allow_schemes=allow_schemes)
    host = (urlparse(url).hostname or "").lower()
    if host in _METADATA_HOSTS or is_internal_host(host):
        raise ValidationError(
            f"{field} host {host!r} is an internal/metadata address; "
            "refusing for SSRF safety.")
    return url
