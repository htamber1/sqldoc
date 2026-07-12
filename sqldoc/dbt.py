"""dbt integration: combine dbt model metadata with the live database schema.

Reads a [dbt](https://www.getdbt.com/) project — ``dbt_project.yml`` plus the
``schema.yml`` files under its model paths — to pull each model's description,
column descriptions, and tests. When a sqldoc-extracted database schema is also
supplied, the two are merged into one unified view per model: the dbt
documentation alongside the *actual* columns, types, and row counts in the
database, highlighting coverage gaps (columns in the DB with no dbt docs) and
drift (columns documented in dbt that no longer exist in the DB).

Metadata only — no table row data is read. YAML parsing uses PyYAML (already a
dependency).
"""
import os
from dataclasses import dataclass, field

import yaml


# --- dbt project model -----------------------------------------------------

@dataclass
class DbtColumn:
    name: str
    description: str = ""
    data_type: str = ""
    tests: list = field(default_factory=list)


@dataclass
class DbtModel:
    name: str
    description: str = ""
    columns: list = field(default_factory=list)   # DbtColumn
    schema: str = ""
    materialized: str = ""
    sql_path: str = ""
    tests: list = field(default_factory=list)      # model-level tests


@dataclass
class DbtProject:
    name: str
    project_dir: str
    profile: str = ""
    model_paths: list = field(default_factory=list)
    models: list = field(default_factory=list)     # DbtModel
    warnings: list = field(default_factory=list)


# --- unified (dbt + database) model ----------------------------------------

@dataclass
class UnifiedColumn:
    name: str
    dbt_description: str = ""
    db_type: str = ""
    db_description: str = ""
    in_dbt: bool = False
    in_db: bool = False
    tests: list = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.in_dbt and self.in_db:
            return "matched"
        if self.in_dbt and not self.in_db:
            return "dbt-only"          # documented but not in the DB (drift)
        return "db-only"              # in the DB but undocumented in dbt


@dataclass
class UnifiedModel:
    name: str
    dbt_description: str = ""
    materialized: str = ""
    matched_table: str = ""           # schema.table in the DB
    in_db: bool = False
    in_dbt: bool = True
    row_count: int = None
    columns: list = field(default_factory=list)   # UnifiedColumn

    @property
    def documented_columns(self) -> int:
        return sum(1 for c in self.columns if c.in_dbt and c.dbt_description)

    @property
    def undocumented_db_columns(self) -> int:
        return sum(1 for c in self.columns if c.in_db and not (c.in_dbt and c.dbt_description))


@dataclass
class DbtDoc:
    project_name: str
    models: list = field(default_factory=list)          # UnifiedModel
    unmatched_db_tables: list = field(default_factory=list)  # schema.table not in dbt
    warnings: list = field(default_factory=list)


# --- discovery + parsing ---------------------------------------------------

def find_dbt_project(start_dir: str = ".") -> str:
    """Return the directory of the nearest dbt project (a dir containing
    ``dbt_project.yml``), searching `start_dir` then its subdirectories one
    level down, else None."""
    start_dir = os.path.abspath(start_dir)
    if os.path.isfile(os.path.join(start_dir, "dbt_project.yml")):
        return start_dir
    try:
        for entry in sorted(os.listdir(start_dir)):
            sub = os.path.join(start_dir, entry)
            if os.path.isdir(sub) and os.path.isfile(os.path.join(sub, "dbt_project.yml")):
                return sub
    except OSError:
        pass
    return None


def _iter_schema_files(project_dir: str, model_paths: list):
    for mp in model_paths:
        root = os.path.join(project_dir, mp)
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                if fn.lower().endswith((".yml", ".yaml")):
                    yield os.path.join(dirpath, fn)


def _find_sql_path(project_dir: str, model_paths: list, model_name: str) -> str:
    target = f"{model_name}.sql".lower()
    for mp in model_paths:
        root = os.path.join(project_dir, mp)
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                if fn.lower() == target:
                    return os.path.join(dirpath, fn)
    return ""


def parse_dbt_project(project_dir: str) -> DbtProject:
    """Parse dbt_project.yml + all schema.yml files into a DbtProject."""
    with open(os.path.join(project_dir, "dbt_project.yml"), encoding="utf-8") as f:
        proj = yaml.safe_load(f) or {}

    name = proj.get("name", os.path.basename(os.path.abspath(project_dir)))
    # dbt v1 uses model-paths; older projects used source-paths.
    model_paths = (proj.get("model-paths") or proj.get("source-paths") or ["models"])
    if isinstance(model_paths, str):
        model_paths = [model_paths]

    project = DbtProject(name=name, project_dir=project_dir,
                         profile=proj.get("profile", ""), model_paths=list(model_paths))

    seen = {}
    for path in _iter_schema_files(project_dir, model_paths):
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except (yaml.YAMLError, OSError) as e:
            project.warnings.append(f"Could not parse {os.path.basename(path)}: {e}")
            continue
        if not isinstance(data, dict):
            continue
        for m in (data.get("models") or []):
            if not isinstance(m, dict) or not m.get("name"):
                continue
            cols = []
            for c in (m.get("columns") or []):
                if not isinstance(c, dict) or not c.get("name"):
                    continue
                cols.append(DbtColumn(
                    name=str(c["name"]),
                    description=str(c.get("description", "") or ""),
                    data_type=str(c.get("data_type", "") or ""),
                    tests=[_test_name(t) for t in (c.get("tests") or [])],
                ))
            config = m.get("config") or {}
            model = DbtModel(
                name=str(m["name"]),
                description=str(m.get("description", "") or ""),
                columns=cols,
                schema=str(config.get("schema", "") or ""),
                materialized=str(config.get("materialized", "") or ""),
                sql_path=_find_sql_path(project_dir, model_paths, str(m["name"])),
                tests=[_test_name(t) for t in (m.get("tests") or [])],
            )
            # Later definitions of the same model name win but merge columns.
            seen[model.name] = model

    project.models = list(seen.values())
    return project


def _test_name(t):
    """A dbt test is either a string ('not_null') or a single-key mapping
    ({'relationships': {...}}); reduce to its name."""
    if isinstance(t, str):
        return t
    if isinstance(t, dict) and t:
        return next(iter(t))
    return str(t)


# --- merge with the live database schema -----------------------------------

def merge(project: DbtProject, tables=None) -> DbtDoc:
    """Combine dbt models with the sqldoc-extracted `tables` (may be None for a
    dbt-only view). Match a dbt model to a DB table by (case-insensitive) name."""
    tables = tables or []
    by_name = {}
    for t in tables:
        by_name.setdefault(t.name.lower(), t)

    doc = DbtDoc(project_name=project.name, warnings=list(project.warnings))
    matched_db = set()

    for model in project.models:
        db = by_name.get(model.name.lower())
        um = UnifiedModel(
            name=model.name,
            dbt_description=model.description,
            materialized=model.materialized,
            in_dbt=True,
        )
        dbt_cols = {c.name.lower(): c for c in model.columns}
        db_cols = {}
        if db is not None:
            um.in_db = True
            um.matched_table = f"{db.schema}.{db.name}"
            um.row_count = getattr(db, "row_count", None)
            matched_db.add(db.name.lower())
            db_cols = {c.name.lower(): c for c in getattr(db, "columns", [])}

        for key in list(dbt_cols) + [k for k in db_cols if k not in dbt_cols]:
            dc = dbt_cols.get(key)
            bc = db_cols.get(key)
            um.columns.append(UnifiedColumn(
                name=(dc.name if dc else bc.name),
                dbt_description=(dc.description if dc else ""),
                db_type=(getattr(bc, "data_type", "") if bc else ""),
                db_description=(getattr(bc, "description", "") or "" if bc else ""),
                in_dbt=dc is not None,
                in_db=bc is not None,
                tests=(dc.tests if dc else []),
            ))
        doc.models.append(um)

    # DB tables with no dbt model at all.
    for t in tables:
        if t.name.lower() not in matched_db and t.name.lower() not in {m.name.lower() for m in project.models}:
            doc.unmatched_db_tables.append(f"{t.schema}.{t.name}")

    return doc


def summarize(doc: DbtDoc) -> dict:
    models = doc.models
    matched = sum(1 for m in models if m.in_db)
    dbt_only = sum(1 for m in models if not m.in_db)
    total_cols = sum(len(m.columns) for m in models)
    documented = sum(m.documented_columns for m in models)
    undocumented = sum(m.undocumented_db_columns for m in models)
    drift = sum(1 for m in models for c in m.columns if c.status == "dbt-only")
    coverage = round(100.0 * documented / total_cols, 1) if total_cols else 0.0
    return {
        "models": len(models),
        "matched_in_db": matched,
        "dbt_only_models": dbt_only,
        "unmatched_db_tables": len(doc.unmatched_db_tables),
        "documented_columns": documented,
        "undocumented_db_columns": undocumented,
        "drifted_columns": drift,
        "doc_coverage_pct": coverage,
    }
