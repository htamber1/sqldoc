"""Regression tests for schema change detection (sqldoc/snapshot.py).

Validated against a real SQL Server instance before being written: a throwaway
database was created, ten representative changes applied, and the reported diff
compared against ground truth. v3.0.4 silently missed four of them.

The bugs these pin, all of the same "silent miss" class -- the tool affirmatively
reported "No schema changes" while real drift had occurred:

  1. Column type recorded as the bare type name ('varchar'), so every
     length/precision/scale change was invisible: varchar(20) -> varchar(50),
     decimal(10,2) -> decimal(18,4), and narrowing, which risks data loss.
  2. Indexes captured in the snapshot but never compared, so adding or dropping
     an index -- including a UNIQUE index -- reported nothing.
  3. Foreign-key targets captured but never compared, so retargeting an FK was
     invisible whenever the referential actions stayed the same.

Plus the upgrade path those fixes create: a pre-existing v1 snapshot must not
produce a spurious type change for every sized column in the database.

These are pure unit tests -- no database required.
"""
import pytest
from types import SimpleNamespace

from sqldoc.snapshot import (SNAPSHOT_VERSION, type_signature, diff_snapshots,
                             format_diff)


def col(data_type, max_length=None, precision=None, scale=None):
    return SimpleNamespace(data_type=data_type, max_length=max_length,
                           precision=precision, scale=scale)


class TestTypeSignature:
    """The fix for bug 1: the snapshot must record width, not just the type."""

    @pytest.mark.parametrize("column,expected", [
        (col("varchar", max_length=50), "varchar(50)"),
        (col("varchar", max_length=20), "varchar(20)"),
        (col("char", max_length=10), "char(10)"),
        (col("varbinary", max_length=64), "varbinary(64)"),
        # nchar/nvarchar store BYTES in max_length; report declared characters.
        (col("nvarchar", max_length=200), "nvarchar(100)"),
        (col("nchar", max_length=8), "nchar(4)"),
        # -1 is the sentinel for (max).
        (col("varchar", max_length=-1), "varchar(max)"),
        (col("nvarchar", max_length=-1), "nvarchar(max)"),
        # decimal/numeric are distinguished by precision+scale, never length:
        # decimal(10,2) and decimal(18,4) are both 9 bytes.
        (col("decimal", max_length=9, precision=10, scale=2), "decimal(10,2)"),
        (col("decimal", max_length=9, precision=18, scale=4), "decimal(18,4)"),
        (col("numeric", max_length=9, precision=5, scale=0), "numeric(5,0)"),
        # fractional-seconds scale
        (col("datetime2", scale=3), "datetime2(3)"),
        (col("time", scale=7), "time(7)"),
        # unsized types stay bare
        (col("int", max_length=4), "int"),
        (col("bit", max_length=1), "bit"),
        (col("uniqueidentifier", max_length=16), "uniqueidentifier"),
    ])
    def test_signature(self, column, expected):
        assert type_signature(column) == expected

    def test_degrades_when_adapter_supplies_no_width(self):
        """Adapters that do not populate max_length/precision must keep working
        and must not start reporting spurious type changes."""
        bare = SimpleNamespace(data_type="varchar")
        assert type_signature(bare) == "varchar"
        assert type_signature(col("decimal")) == "decimal"

    def test_decimal_precision_change_is_visible(self):
        """The specific case max_length alone cannot catch."""
        assert type_signature(col("decimal", 9, 10, 2)) != \
               type_signature(col("decimal", 9, 18, 4))


# --- snapshot fixtures ------------------------------------------------------

def snap(tables, version=SNAPSHOT_VERSION):
    return {"version": version, "database": "testdb", "tables": tables,
            "views": {}, "procedures": {}}


def table(columns, indexes=None):
    return {"row_count": 0, "columns": columns, "indexes": indexes or {},
            "checks": {}, "uniques": {}}


def column(type_, nullable=True, **extra):
    return dict({"type": type_, "nullable": nullable}, **extra)


def index(columns, unique=False, primary_key=False, type_="NONCLUSTERED",
          included=None):
    return {"type": type_, "unique": unique, "primary_key": primary_key,
            "columns": list(columns), "included": list(included or [])}


class TestDiffDetects:
    """One test per change class, mirroring the changes made on real hardware."""

    def test_no_changes_reports_none(self):
        s = snap({"dbo.t": table({"a": column("int", False)})})
        d = diff_snapshots(s, s)
        assert d["has_changes"] is False
        assert "No schema changes" in format_diff(d)

    def test_table_added_and_removed(self):
        old = snap({"dbo.keep": table({"a": column("int")}),
                    "dbo.gone": table({"a": column("int")})})
        new = snap({"dbo.keep": table({"a": column("int")}),
                    "dbo.fresh": table({"a": column("int")})})
        d = diff_snapshots(old, new)
        assert d["tables_added"] == ["dbo.fresh"]
        assert d["tables_removed"] == ["dbo.gone"]

    def test_column_added_and_removed(self):
        old = snap({"dbo.t": table({"keep": column("int"), "drop_me": column("int")})})
        new = snap({"dbo.t": table({"keep": column("int"), "new_col": column("int")})})
        mod = diff_snapshots(old, new)["tables_modified"][0]
        assert mod["added"] == ["new_col"]
        assert mod["removed"] == ["drop_me"]

    def test_column_type_change(self):
        old = snap({"dbo.t": table({"s": column("varchar(20)")})})
        new = snap({"dbo.t": table({"s": column("varchar(50)")})})
        mod = diff_snapshots(old, new)["tables_modified"][0]
        assert ("type", "varchar(20)", "varchar(50)") in mod["changed"][0]["deltas"]

    def test_column_narrowing_is_detected(self):
        """Narrowing is the data-loss case and must never be silent."""
        old = snap({"dbo.t": table({"s": column("varchar(500)")})})
        new = snap({"dbo.t": table({"s": column("varchar(50)")})})
        assert diff_snapshots(old, new)["has_changes"] is True

    def test_nullability_change(self):
        old = snap({"dbo.t": table({"a": column("decimal(10,2)", nullable=False)})})
        new = snap({"dbo.t": table({"a": column("decimal(10,2)", nullable=True)})})
        mod = diff_snapshots(old, new)["tables_modified"][0]
        assert ("nullable", False, True) in mod["changed"][0]["deltas"]

    def test_type_and_nullability_reported_independently(self):
        """A column changed in two ways must report both deltas, not one."""
        old = snap({"dbo.t": table({"a": column("decimal(10,2)", nullable=False)})})
        new = snap({"dbo.t": table({"a": column("decimal(18,4)", nullable=True)})})
        deltas = diff_snapshots(old, new)["tables_modified"][0]["changed"][0]["deltas"]
        assert ("type", "decimal(10,2)", "decimal(18,4)") in deltas
        assert ("nullable", False, True) in deltas

    def test_index_added_and_removed(self):
        old = snap({"dbo.t": table({"a": column("int")},
                                   {"IX_old": index(["a"])})})
        new = snap({"dbo.t": table({"a": column("int")},
                                   {"IX_new": index(["a"])})})
        mod = diff_snapshots(old, new)["tables_modified"][0]
        assert mod["indexes_added"] == ["IX_new"]
        assert mod["indexes_removed"] == ["IX_old"]

    def test_index_column_change(self):
        old = snap({"dbo.t": table({"a": column("int"), "b": column("int")},
                                   {"IX": index(["a"])})})
        new = snap({"dbo.t": table({"a": column("int"), "b": column("int")},
                                   {"IX": index(["a", "b"])})})
        mod = diff_snapshots(old, new)["tables_modified"][0]
        assert mod["indexes_changed"][0]["name"] == "IX"

    def test_index_uniqueness_change_is_a_constraint_change(self):
        old = snap({"dbo.t": table({"a": column("int")}, {"IX": index(["a"])})})
        new = snap({"dbo.t": table({"a": column("int")},
                                   {"IX": index(["a"], unique=True)})})
        mod = diff_snapshots(old, new)["tables_modified"][0]
        assert ("unique", False, True) in mod["indexes_changed"][0]["deltas"]

    def test_foreign_key_retarget(self):
        """Retargeting an FK with the referential actions held constant."""
        old = snap({"dbo.t": table(
            {"fk": column("int", references="customers.customer_id",
                          on_delete="NO_ACTION", on_update="NO_ACTION")})})
        new = snap({"dbo.t": table(
            {"fk": column("int", references="orders.order_id",
                          on_delete="NO_ACTION", on_update="NO_ACTION")})})
        mod = diff_snapshots(old, new)["tables_modified"][0]
        assert ("references", "customers.customer_id", "orders.order_id") \
            in mod["changed"][0]["deltas"]


class TestNoFalsePositives:

    def test_identical_snapshots_with_every_feature_populated(self):
        s = snap({"dbo.t": table(
            {"id": column("int", False, pk=True),
             "name": column("nvarchar(100)", False),
             "amount": column("decimal(18,4)"),
             "fk": column("int", references="other.id", on_delete="CASCADE")},
            {"PK_t": index(["id"], unique=True, primary_key=True, type_="CLUSTERED"),
             "IX_name": index(["name"], included=["amount"])})})
        assert diff_snapshots(s, s)["has_changes"] is False

    def test_row_count_change_alone_is_not_schema_drift(self):
        old = snap({"dbo.t": table({"a": column("int")})})
        new = snap({"dbo.t": table({"a": column("int")})})
        new["tables"]["dbo.t"]["row_count"] = 1_000_000
        assert diff_snapshots(old, new)["has_changes"] is False


class TestUpgradePath:
    """Version 1 snapshots stored bare type names. Comparing them against v2's
    precise types would report a spurious change for every sized column."""

    def test_v1_baseline_does_not_produce_spurious_type_changes(self):
        old = snap({"dbo.t": table({"s": column("varchar"),
                                    "n": column("nvarchar"),
                                    "d": column("decimal")})}, version=1)
        new = snap({"dbo.t": table({"s": column("varchar(50)"),
                                    "n": column("nvarchar(100)"),
                                    "d": column("decimal(18,4)")})})
        d = diff_snapshots(old, new)
        assert d["has_changes"] is False, (
            "Upgrading from a v1 snapshot reported type changes for columns "
            "that did not change; every sized column in the database would be "
            "flagged on the first run after upgrade.")
        assert d["types_not_compared"] is True
        assert "predates precise column types" in format_diff(d)

    def test_v1_baseline_still_reports_real_structural_changes(self):
        """Suppressing type deltas must not suppress everything else."""
        old = snap({"dbo.t": table({"s": column("varchar"), "gone": column("int")})},
                   version=1)
        new = snap({"dbo.t": table({"s": column("varchar(50)"),
                                    "added": column("int")})})
        mod = diff_snapshots(old, new)["tables_modified"][0]
        assert mod["added"] == ["added"]
        assert mod["removed"] == ["gone"]
        assert not mod["changed"], "type delta should be suppressed for a v1 baseline"

    def test_v2_baseline_compares_types_normally(self):
        old = snap({"dbo.t": table({"s": column("varchar(20)")})})
        new = snap({"dbo.t": table({"s": column("varchar(50)")})})
        d = diff_snapshots(old, new)
        assert d["types_not_compared"] is False
        assert d["has_changes"] is True

    def test_missing_or_malformed_version_is_treated_as_v1(self):
        old = snap({"dbo.t": table({"s": column("varchar")})})
        del old["version"]
        new = snap({"dbo.t": table({"s": column("varchar(50)")})})
        assert diff_snapshots(old, new)["types_not_compared"] is True

        old2 = snap({"dbo.t": table({"s": column("varchar")})}, version="not-a-number")
        assert diff_snapshots(old2, new)["types_not_compared"] is True
