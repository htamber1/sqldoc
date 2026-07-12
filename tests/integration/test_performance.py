"""Phase 9 — performance against the live SQL Server (AdventureWorks2022, 71
tables). Verifies `doc --no-ai` completes well under budget, and that repeated
runs don't leak memory. Skip-gated on SQL Server + psutil."""
import gc
import time

import pytest

from _live import MSSQL_CS, requires_mssql, run

psutil = pytest.importorskip("psutil")

pytestmark = [requires_mssql, pytest.mark.performance, pytest.mark.integration]

BASE = ["--connection-string", MSSQL_CS, "--dialect", "sqlserver",
        "--no-ai", "--no-snapshot", "--no-cache"]

# Generous vs. the observed ~1s so it never flakes, but still enforces the budget.
TIME_BUDGET_S = 60.0


def _doc(tmp_path, i=0):
    out = str(tmp_path / f"perf-{i}.html")
    r = run(["doc", *BASE, "--output", out])
    assert r.exit_code == 0, r.output
    return out


def test_doc_under_budget(tmp_path):
    t0 = time.perf_counter()
    out = _doc(tmp_path)
    elapsed = time.perf_counter() - t0
    assert elapsed < TIME_BUDGET_S, f"doc took {elapsed:.1f}s (budget {TIME_BUDGET_S}s)"
    # a real, complete report was produced
    import os
    assert os.path.getsize(out) > 100_000


def test_no_memory_leak_across_runs(tmp_path):
    proc = psutil.Process()
    rss = []
    for i in range(5):
        _doc(tmp_path, i)
        gc.collect()
        rss.append(proc.memory_info().rss / 1e6)   # MB
    # Ignore the first run (import/cache warmup); steady-state growth must be small.
    steady_growth = rss[-1] - rss[1]
    assert steady_growth < 40, f"RSS grew {steady_growth:.1f} MB over 4 steady runs: {rss}"
    # absolute footprint stays reasonable
    assert max(rss) < 600, f"peak RSS {max(rss):.1f} MB too high"


def test_doc_and_scan_throughput(tmp_path):
    """doc + scan on the full 71-table database both finish within budget."""
    for cmd, extra in (("doc", BASE), ("scan", ["--connection-string", MSSQL_CS,
                                                "--dialect", "sqlserver"])):
        t0 = time.perf_counter()
        out = str(tmp_path / f"{cmd}.html")
        r = run([cmd, *extra, "--output", out])
        assert r.exit_code == 0, r.output
        assert time.perf_counter() - t0 < TIME_BUDGET_S
