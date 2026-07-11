"""SARIF 2.1.0 export for PII findings.

Emits a SARIF log that GitHub Advanced Security (code scanning) and Azure
DevOps can import directly, so PII findings show up alongside other security
results and can gate CI.
"""
import re
import json

# Risk -> SARIF result level.
_LEVEL = {"HIGH": "error", "MEDIUM": "warning", "LOW": "note"}
INFO_URI = "https://github.com/htamber1/sqldoc"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("sqldoc")
    except Exception:
        return "1.1.0"


def build_sarif(database: str, findings: list) -> dict:
    rules = {}
    results = []
    for f in findings:
        rid = _slug(f.category)
        level = _LEVEL.get(f.risk, "warning")
        if rid not in rules:
            rules[rid] = {
                "id": rid,
                "name": f.category.replace(" ", "").replace("/", ""),
                "shortDescription": {"text": f"Likely {f.category} data"},
                "fullDescription": {"text": f"Columns that likely contain {f.category}. "
                                            f"Regulations: {', '.join(f.regulations)}."},
                "help": {"text": f.action},
                "defaultConfiguration": {"level": level},
                "properties": {"tags": ["pii", "compliance", *f.regulations]},
            }
        loc = f"{f.schema}.{f.table}.{f.column}"
        results.append({
            "ruleId": rid,
            "level": level,
            "message": {"text": f"{f.category} likely in {loc} "
                                f"({', '.join(f.regulations)}). {f.action}"},
            "locations": [{
                "physicalLocation": {
                    "artifactLocation": {"uri": f"{f.schema}/{f.table}"},
                    "region": {"startLine": 1},
                },
                "logicalLocations": [{
                    "fullyQualifiedName": loc, "name": f.column, "kind": "member",
                }],
            }],
            "properties": {
                "risk": f.risk, "confidence": f.confidence,
                "confidenceScore": f.confidence_score,
                "dataType": f.data_type, "regulations": f.regulations,
            },
        })

    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {
                "name": "sqldoc",
                "informationUri": INFO_URI,
                "version": _version(),
                "rules": list(rules.values()),
            }},
            "results": results,
            "properties": {"database": database},
        }],
    }


def render_sarif(database: str, findings: list, output_path: str):
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(build_sarif(database, findings), f, indent=2)
    print(f"SARIF report written to {output_path}")
