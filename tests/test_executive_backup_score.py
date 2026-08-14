"""`executive.backup_compliance` must judge backup AGE, not just issue rules.

Regression cover for a field bug: the score documented itself as measuring a
"non-stale" posture but never checked age. `issues` only ever held *never backed
up*, *FULL/BULK_LOGGED without log backups* and *SIMPLE recovery* -- so a
database on FULL recovery with current log backups and a TWO-YEAR-OLD full
backup raised no rule at all and scored 100% compliant.

Staleness is now judged by `backup.stale_databases`, the same rule the agent's
`backup_stale` alert uses, via a shared `DEFAULT_MAX_BACKUP_AGE_HOURS`.

Pure functions; no database required.
"""
import pytest

from sqldoc.backup import (BackupReport, DatabaseBackup,
                           DEFAULT_MAX_BACKUP_AGE_HOURS, stale_databases)
from sqldoc.executive import backup_compliance

TWO_YEARS_HOURS = 24 * 365 * 2


def db(name, **kw):
    """A fully compliant database by default; override fields per test."""
    base = dict(recovery_model="FULL", last_full_backup="2026-08-13 02:00:00",
                last_log_backup="2026-08-13 03:00:00", last_backup_age_hours=1.0,
                never_backed_up=False, pitr_capable=True)
    base.update(kw)
    return DatabaseBackup(database=name, **base)


def report(*dbs, **kw):
    return BackupReport(dialect="sqlserver", databases=list(dbs),
                        pitr_enabled=kw.get("pitr_enabled", True),
                        supported=kw.get("supported", True))


# --- the bug ----------------------------------------------------------------

def test_stale_full_backup_does_not_score_as_compliant():
    r = report(db("Prod", last_backup_age_hours=TWO_YEARS_HOURS))
    assert backup_compliance(r) == 0


def test_stale_databases_agrees_it_is_stale():
    """The score and the agent alert must not disagree about the same database."""
    r = report(db("Prod", last_backup_age_hours=TWO_YEARS_HOURS))
    assert [d.database for d in stale_databases(r, 24.0)] == ["Prod"]


# --- no regression on the pre-existing rules --------------------------------

def test_fresh_backup_with_no_issues_is_fully_compliant():
    assert backup_compliance(report(db("Prod"))) == 100


def test_never_backed_up_scores_zero():
    r = report(db("Prod", never_backed_up=True, last_full_backup="",
                  last_backup_age_hours=None))
    assert backup_compliance(r) == 0


def test_a_database_with_issues_scores_zero():
    r = report(DatabaseBackup(database="Prod", recovery_model="SIMPLE",
                              last_full_backup="2026-08-13",
                              last_backup_age_hours=1.0,
                              issues=["SIMPLE recovery model."]))
    assert backup_compliance(r) == 0


@pytest.mark.parametrize("stale_count,expected", [(1, 50), (3, 25)])
def test_mixed_estates_score_proportionally(stale_count, expected):
    dbs = [db("Fresh")] + [db(f"Stale{i}", last_backup_age_hours=TWO_YEARS_HOURS)
                           for i in range(stale_count)]
    assert backup_compliance(report(*dbs)) == expected


def test_same_named_entries_are_counted_individually():
    """Staleness is matched per entry, so a repeated database name cannot drag
    an otherwise-healthy entry into the stale set."""
    r = report(db("Prod"), db("Prod", last_backup_age_hours=TWO_YEARS_HOURS))
    assert backup_compliance(r) == 50


# --- the threshold ----------------------------------------------------------

def test_default_threshold_constant_is_24_hours():
    assert DEFAULT_MAX_BACKUP_AGE_HOURS == 24.0


def test_exactly_at_the_threshold_is_not_stale():
    assert backup_compliance(report(db("Prod", last_backup_age_hours=24.0))) == 100


def test_just_over_the_threshold_is_stale():
    assert backup_compliance(report(db("Prod", last_backup_age_hours=24.1))) == 0


def test_caller_supplied_threshold_is_honoured():
    r = report(db("Prod", last_backup_age_hours=168.0))
    assert backup_compliance(r, max_age_hours=336.0) == 100
    assert backup_compliance(r) == 0


def test_unknown_age_is_not_treated_as_stale():
    """An unreadable age must not be scored as a failure -- it matches what
    stale_databases does, which skips a None age."""
    assert backup_compliance(report(db("Prod", last_backup_age_hours=None))) == 100


# --- degenerate inputs preserved --------------------------------------------

def test_no_report_returns_none():
    assert backup_compliance(None) is None


def test_unsupported_dialect_returns_none():
    assert backup_compliance(report(supported=False)) is None


@pytest.mark.parametrize("pitr,expected", [(True, 100), (False, 0)])
def test_no_databases_falls_back_to_the_pitr_flag(pitr, expected):
    assert backup_compliance(report(pitr_enabled=pitr)) == expected
