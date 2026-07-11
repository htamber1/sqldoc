"""JSON renderer: machine-readable schema export for programmatic consumers.

Unlike ``snapshot.py`` (structure-only, for diffing) this emits the *complete*
extracted model — AI descriptions, index/trigger/constraint detail and all — so
downstream tooling (catalogs, code generators, data-contract checks) can consume
a database's schema without scraping the HTML/Markdown reports.

The dataclasses are serialized with ``dataclasses.asdict`` so any field added to
the extractor model appears here automatically — there is nothing per-field to
maintain in this renderer.
"""
import json
from dataclasses import asdict
from datetime import datetime

from sqldoc import __version__

JSON_SCHEMA_VERSION = 1


def build_json(database, tables, views=None, procedures=None) -> dict:
    views = views or []
    procedures = procedures or []
    return {
        "schema_version": JSON_SCHEMA_VERSION,
        "sqldoc_version": __version__,
        "database": database,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "stats": {
            "tables": len(tables),
            "views": len(views),
            "procedures": len(procedures),
            "columns": sum(len(t.columns) for t in tables),
        },
        "tables": [asdict(t) for t in tables],
        "views": [asdict(v) for v in views],
        "procedures": [asdict(p) for p in procedures],
    }


def render_json(database, tables, output_path, views=None, procedures=None):
    data = build_json(database, tables, views, procedures)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    print(f"Documentation written to {output_path}")
