"""Dialect-neutral core of the extraction pipeline.

This module defines two things that the whole pipeline flows through:

* The **shared dataclasses** (`Table`, `Column`, `Index`, ...) — the
  dialect-neutral "currency" that the extractor populates, the AI stage
  enriches, and every renderer/analysis module reads. They live here (not in a
  concrete adapter) precisely so that a Postgres/MySQL/Azure adapter can produce
  the same shapes and the rest of the pipeline never learns which database it
  came from.

* The **`DatabaseAdapter` ABC** — the metadata contract every dialect must
  satisfy (`extract_metadata`/`extract_views`/`extract_procedures`), plus a
  `Capabilities` advertisement of which higher-level commands (`health`,
  `quality`, `comply` access-audit, ...) that dialect can actually serve. A
  command consults `capabilities` and renders an explicit "not available on
  <dialect>" section rather than emitting wrong SQL.

`sqldoc.extractor` re-exports the dataclasses and thin free-function wrappers
for backward compatibility, so existing `from sqldoc.extractor import Table`
imports keep working unchanged.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Optional


# --- Shared dataclasses (the dialect-neutral currency) ---------------------

@dataclass
class Column:
    name: str
    data_type: str
    max_length: Optional[int]
    is_nullable: bool
    is_primary_key: bool
    is_foreign_key: bool
    references_table: Optional[str]
    references_column: Optional[str]
    description: Optional[str] = None
    is_computed: bool = False
    computed_definition: Optional[str] = None
    default_definition: Optional[str] = None      # DEFAULT constraint expression, e.g. "((0))"
    fk_on_delete: Optional[str] = None            # NO_ACTION / CASCADE / SET_NULL / SET_DEFAULT
    fk_on_update: Optional[str] = None


@dataclass
class Index:
    name: str
    type_desc: str          # CLUSTERED / NONCLUSTERED / HEAP
    is_unique: bool
    is_primary_key: bool
    key_columns: list[str] = field(default_factory=list)
    included_columns: list[str] = field(default_factory=list)


@dataclass
class Trigger:
    name: str
    is_instead_of: bool     # INSTEAD OF vs AFTER
    is_disabled: bool
    events: list[str] = field(default_factory=list)   # INSERT / UPDATE / DELETE
    definition: Optional[str] = None


@dataclass
class CheckConstraint:
    name: str
    definition: str                 # the CHECK expression, e.g. "([Qty]>(0))"
    column: Optional[str] = None    # owning column, or None for a table-level check


@dataclass
class UniqueConstraint:
    name: str
    columns: list[str] = field(default_factory=list)


@dataclass
class Table:
    schema: str
    name: str
    row_count: int
    columns: list[Column] = field(default_factory=list)
    indexes: list[Index] = field(default_factory=list)
    triggers: list[Trigger] = field(default_factory=list)
    check_constraints: list[CheckConstraint] = field(default_factory=list)
    unique_constraints: list[UniqueConstraint] = field(default_factory=list)
    description: Optional[str] = None


@dataclass
class View:
    schema: str
    name: str
    columns: list[Column] = field(default_factory=list)
    definition: Optional[str] = None
    description: Optional[str] = None


@dataclass
class Parameter:
    name: str
    data_type: str
    max_length: Optional[int]
    is_output: bool


@dataclass
class StoredProcedure:
    schema: str
    name: str
    parameters: list[Parameter] = field(default_factory=list)
    definition: Optional[str] = None
    description: Optional[str] = None


# --- Per-dialect capability advertisement ----------------------------------

@dataclass(frozen=True)
class Capabilities:
    """What a given dialect can serve. The metadata surface (`documentation`)
    is table stakes; the flags below vary because they lean on dialect-specific
    catalogs (e.g. SQL Server DMVs for `health`, `sys.database_permissions` for
    the `comply` access audit) that have no exact analogue everywhere.

    Commands should check the relevant flag and, when False, render an explicit
    "not available on <dialect>" section instead of running wrong SQL. Defaults
    are conservative (only the dialect-neutral pieces default True) so a new
    adapter opts in to each dialect-specific feature deliberately.
    """
    documentation: bool = True       # doc: tables/columns/views/procs
    triggers: bool = True            # trigger extraction
    computed_columns: bool = True    # computed-column definitions
    check_constraints: bool = True   # CHECK / UNIQUE constraint extraction
    pii_scan: bool = True            # scan — runs on the populated dataclasses
    intel: bool = True               # intel — metadata-only
    insights: bool = True            # insights — metadata + heuristics
    data_lineage: bool = True        # comply lineage — parses view/proc bodies
    comply_regulations: bool = True  # comply per-regulation sections
    quality: bool = False            # aggregate profiling (needs dialect SQL)
    health: bool = False             # DMV performance/health checks
    access_audit: bool = False       # comply access audit over object grants


class DatabaseAdapter(ABC):
    """Metadata contract for one SQL dialect.

    Concrete adapters (see `sqlserver.py`) implement the three `extract_*`
    methods against their catalog, populating the shared dataclasses above.
    Connection acquisition goes through `connect()`, which honours an injected
    connector (used by the `sqldoc.extractor` shim so that tests can monkeypatch
    a fake connection) and otherwise falls back to the adapter's own driver.
    """

    dialect: str = ""
    display_name: str = ""
    capabilities: Capabilities = Capabilities()

    def __init__(self, connection_string: str, connect: Optional[Callable] = None):
        self.connection_string = connection_string
        self._connect_override = connect

    def connect(self):
        """Open a DBAPI connection. Prefers an injected connector, else the
        adapter's own driver via `_default_connect`."""
        if self._connect_override is not None:
            return self._connect_override(self.connection_string)
        return self._default_connect(self.connection_string)

    @staticmethod
    def _default_connect(connection_string: str):
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def build_connection_string(server: str, database: str,
                                username: str, password: str) -> str:
        """Assemble a driver-appropriate connection string from discrete parts."""

    @abstractmethod
    def extract_metadata(self) -> list[Table]:
        """Tables with columns, indexes, triggers, and constraints."""

    @abstractmethod
    def extract_views(self) -> list[View]:
        """Views with columns and (locally-rendered) definitions."""

    @abstractmethod
    def extract_procedures(self) -> list[StoredProcedure]:
        """Stored procedures with parameters and definitions."""
