"""SARIF 2.1.0 export."""
from sqldoc.sarif import build_sarif
from sqldoc.pii import Finding


def _findings():
    return [
        Finding("dbo", "Users", "SSN", "nvarchar", "National ID / SSN", "HIGH",
                "name + type match", ["GDPR", "HIPAA"], "Encrypt at rest."),
        Finding("dbo", "Users", "Email", "nvarchar", "Email Address", "MEDIUM",
                "name + type match", ["GDPR"], "Collect with consent."),
        Finding("dbo", "Users", "Login", "nvarchar", "Online Identifier", "LOW",
                "name match", ["GDPR"], "Account identifier."),
    ]


def test_sarif_top_level_shape():
    s = build_sarif("DB", _findings())
    assert s["version"] == "2.1.0"
    assert s["$schema"].endswith("sarif-2.1.0.json")
    driver = s["runs"][0]["tool"]["driver"]
    assert driver["name"] == "sqldoc"
    assert s["runs"][0]["properties"]["database"] == "DB"


def test_sarif_results_and_levels():
    run = build_sarif("DB", _findings())["runs"][0]
    assert len(run["results"]) == 3
    levels = sorted(r["level"] for r in run["results"])
    assert levels == ["error", "note", "warning"]   # HIGH/LOW/MEDIUM


def test_sarif_rules_deduped_per_category():
    run = build_sarif("DB", _findings())["runs"][0]
    ids = {rule["id"] for rule in run["tool"]["driver"]["rules"]}
    assert ids == {"national-id-ssn", "email-address", "online-identifier"}


def test_sarif_result_location():
    run = build_sarif("DB", _findings())["runs"][0]
    r = run["results"][0]
    assert r["ruleId"] == "national-id-ssn"
    loc = r["locations"][0]
    assert loc["logicalLocations"][0]["fullyQualifiedName"] == "dbo.Users.SSN"
    assert loc["physicalLocation"]["artifactLocation"]["uri"] == "dbo/Users"
    assert r["properties"]["regulations"] == ["GDPR", "HIPAA"]
