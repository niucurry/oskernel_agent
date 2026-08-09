from __future__ import annotations

from oskernel_agent.comparison.buildlib.coverage import audit_entries
from oskernel_agent.comparison.ingest.config import RepoEntry
from oskernel_agent.comparison.models import FunctionRecord, ModuleTag
from oskernel_agent.comparison.normalize.store import FunctionStore
from oskernel_agent.comparison.retrieval_contract import build_retrieval_contract, contract_errors


def _entry(team: str) -> RepoEntry:
    return RepoEntry(repo_url=f"https://example.com/{team}", year=2025, team_name=team)


def test_coverage_reports_each_missing_configured_repo(tmp_path):
    db = tmp_path / "functions.db"
    with FunctionStore(db) as store:
        rec = FunctionRecord(
            repo_id="2025/present", file_path="os/a.rs", start_line=1, end_line=10,
            func_name="run", module_tag=ModuleTag.SCHED, lang="rust",
            raw_code="fn run() {}", normalized_code="fn FUNC_0 ( ) { }",
        )
        store.write_repo("2025/present", [(rec, [], [])])

    audit = audit_entries(db, [_entry("present"), _entry("missing")])
    assert not audit.complete
    assert audit.covered == 1 and audit.configured == 2
    assert audit.missing_repo_ids == ("2025/missing",)


def test_coverage_complete_only_when_every_repo_has_functions(tmp_path):
    db = tmp_path / "functions.db"
    with FunctionStore(db) as store:
        for team in ("a", "b"):
            rec = FunctionRecord(
                repo_id=f"2025/{team}", file_path="os/a.rs", start_line=1, end_line=10,
                func_name="run", module_tag=ModuleTag.SCHED, lang="rust",
                raw_code="fn run() {}", normalized_code=f"fn {team} ( ) {{ }}",
            )
            store.write_repo(f"2025/{team}", [(rec, [], [])])
    assert audit_entries(db, [_entry("a"), _entry("b")]).complete


def test_retrieval_contract_requires_all_channels_and_exact_history_counts():
    good = build_retrieval_contract({
        "complete": True, "configured": 2, "covered": 2,
        "missing_repo_ids": [],
    }, complete=True)
    assert contract_errors(good) == []
    assert "function_identity_neighbor" in good["channels"]

    bad = build_retrieval_contract({
        "complete": True, "configured": 2, "covered": 1,
        "missing_repo_ids": [],
    }, complete=True)
    bad["channels"].remove("normalized_code_simhash")
    bad["same_language_only"] = False
    errors = contract_errors(bad)
    assert any("结构" not in e and "normalized_code_simhash" in e for e in errors)
    assert any("1/2" in e for e in errors)
    assert any("同一编程语言" in e for e in errors)
