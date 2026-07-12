"""Google BigQuery adapter.

BigQuery is accessed through the ``google-cloud-bigquery`` client library (not a
DBAPI cursor), so this adapter uses the client API directly:

* Datasets → tables/views/routines via ``list_datasets`` / ``list_tables`` /
  ``list_routines`` + ``get_table``.
* Each table's **partitioning** (time / integer-range) and **clustering fields**,
  plus **storage stats** (size, row count) and **last-modified** time, folded
  into the table description.
* Views expose their ``view_query``; routines become stored procedures.

The client is an *optional* dependency imported lazily; a missing driver raises a
clear ``pip install sqldoc[bigquery]`` error. Detected from a ``bigquery://``
scheme (``bigquery://<project>``).

NOTE: mock-tested only — not run against a live BigQuery project.
"""
from urllib.parse import urlparse

from sqldoc.adapters.base import (
    DatabaseAdapter, Capabilities, Table, Column, View, StoredProcedure,
)


def _s(v):
    return "" if v is None else str(v)


class BigQueryAdapter(DatabaseAdapter):
    dialect = "bigquery"
    display_name = "Google BigQuery"
    capabilities = Capabilities(quality=False, health=False, access_audit=False,
                                triggers=False, computed_columns=False, check_constraints=False)

    @staticmethod
    def _default_connect(connection_string: str):
        try:
            from google.cloud import bigquery
        except ImportError as e:
            raise ImportError(
                "BigQuery support requires the 'google-cloud-bigquery' library, "
                "which is not installed. Install it with:  pip install sqldoc[bigquery]  "
                "(or:  pip install google-cloud-bigquery)."
            ) from e
        u = urlparse(connection_string)
        project = u.hostname or (u.path.lstrip("/").split("/")[0] if u.path else None)
        return bigquery.Client(project=project)

    @staticmethod
    def build_connection_string(server: str, database: str,
                                username: str, password: str) -> str:
        return f"bigquery://{server or database}"

    def _get_full(self, client, item):
        return client.get_table(getattr(item, "reference", item))

    @staticmethod
    def _columns(schema_fields) -> list:
        cols = []
        for f in schema_fields or []:
            mode = _s(getattr(f, "mode", "NULLABLE")).upper()
            cols.append(Column(
                name=_s(getattr(f, "name", "")),
                data_type=_s(getattr(f, "field_type", "")) + ("[]" if mode == "REPEATED" else ""),
                max_length=None,
                is_nullable=(mode != "REQUIRED"),
                is_primary_key=False, is_foreign_key=False,
                references_table=None, references_column=None,
                description=(_s(getattr(f, "description", "")) or None),
            ))
        return cols

    @staticmethod
    def _table_tag(t) -> str:
        parts = []
        tp = getattr(t, "time_partitioning", None)
        rp = getattr(t, "range_partitioning", None)
        if tp is not None:
            field = getattr(tp, "field", None) or "_PARTITIONTIME"
            parts.append(f"partitioned by {field} ({_s(getattr(tp, 'type_', 'DAY'))})")
        elif rp is not None:
            parts.append(f"range-partitioned by {_s(getattr(rp, 'field', ''))}")
        clustering = getattr(t, "clustering_fields", None)
        if clustering:
            parts.append("clustered by " + ", ".join(clustering))
        num_bytes = getattr(t, "num_bytes", None)
        if num_bytes:
            parts.append(f"{round(num_bytes / 1073741824.0, 2)} GB")
        modified = getattr(t, "modified", None)
        if modified:
            parts.append(f"modified {_s(modified)[:19]}")
        return "[BigQuery: " + ", ".join(parts) + "]" if parts else ""

    # --- tables ------------------------------------------------------------

    def extract_metadata(self) -> list[Table]:
        client = self.connect()
        tables = []
        for ds in client.list_datasets():
            dsid = _s(getattr(ds, "dataset_id", ds))
            for item in client.list_tables(dsid):
                t = self._get_full(client, item)
                if _s(getattr(t, "table_type", "TABLE")).upper() == "VIEW":
                    continue
                desc = _s(getattr(t, "description", "")) or None
                tag = self._table_tag(t)
                if tag:
                    desc = (tag + " " + (desc or "")).strip()
                tables.append(Table(
                    schema=dsid, name=_s(getattr(t, "table_id", "")),
                    row_count=int(getattr(t, "num_rows", 0) or 0),
                    columns=self._columns(getattr(t, "schema", [])),
                    indexes=[], triggers=[], check_constraints=[],
                    unique_constraints=[], description=desc,
                ))
        return tables

    # --- views -------------------------------------------------------------

    def extract_views(self) -> list[View]:
        client = self.connect()
        views = []
        for ds in client.list_datasets():
            dsid = _s(getattr(ds, "dataset_id", ds))
            for item in client.list_tables(dsid):
                t = self._get_full(client, item)
                if _s(getattr(t, "table_type", "TABLE")).upper() != "VIEW":
                    continue
                views.append(View(
                    schema=dsid, name=_s(getattr(t, "table_id", "")),
                    columns=self._columns(getattr(t, "schema", [])),
                    definition=(_s(getattr(t, "view_query", "")) or None),
                ))
        return views

    # --- routines ----------------------------------------------------------

    def extract_procedures(self) -> list[StoredProcedure]:
        client = self.connect()
        procs = []
        for ds in client.list_datasets():
            dsid = _s(getattr(ds, "dataset_id", ds))
            if not hasattr(client, "list_routines"):
                continue
            for r in client.list_routines(dsid):
                procs.append(StoredProcedure(
                    schema=dsid, name=_s(getattr(r, "routine_id", "")),
                    parameters=[], definition=(_s(getattr(r, "body", "")) or None)))
        return procs
