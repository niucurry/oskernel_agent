from __future__ import annotations

from datetime import datetime

import pytest

from oskernel_agent.finals import development
from tests.history_controls import (
    build_controls, git, known_numstat, neutral_ai,
)


@pytest.fixture(scope="module")
def histories(tmp_path_factory):
    return build_controls(tmp_path_factory.mktemp("known-history"))


def test_shallow_boundary_keeps_raw_observation_and_known_original_parent(histories):
    case = histories["shallow_boundary"]
    truth = known_numstat(case["origin"], case["parent"], case["boundary"])
    commits, shallow = development.collect_commits(case["root"])
    boundary = next(c for c in commits if c["sha"] == case["boundary"])
    assert truth == 2  # One replacement, known from the complete upstream history.
    assert shallow and len(commits) == 2
    assert boundary["parents"] == [case["parent"]]
    assert boundary["change_metrics_basis"] == "shallow_boundary_unresolved"
    assert not boundary["change_metrics_complete"]
    assert boundary["additions"] is None and boundary["deletions"] is None
    assert boundary["files"] == []
    assert boundary["numstat_observation"]["additions"] == 1202
    assert [c["sha"] for c in commits] == [case["boundary"], case["head"]]
    assert commits[1]["additions"] + commits[1]["deletions"] == 2


def test_boundary_does_not_create_large_commit_or_commit_count_violation(histories):
    commits, shallow = development.collect_commits(histories["shallow_boundary"]["root"])
    evidence = development.build_development_evidence(commits, shallow=shallow, min_commits=3)
    candidates = {c["candidate_id"]: c for c in evidence["candidates"]}
    assert "large-commits" not in candidates
    assert candidates["commit-count"]["kind"] == "提交次数待核对"
    assert "不能判定实际提交次数不足" in candidates["commit-count"]["fact"]
    assert evidence["unknown_change_commit_count"] == 1
    assert evidence["timeline"][0]["loc"] is None
    analysis = development.analyze_history("control", commits, neutral_ai(commits, evidence),
                                           shallow=shallow, min_commits=3)
    stage = analysis["stages"][0]
    assert stage["loc"] == 2 and stage["unknown_change_commit_count"] == 1
    assert stage["files"] == [{"path": "source.c", "loc": 2}]
    html = development.render_development_html(analysis)
    assert "可核对变更 2 LOC；1 次提交变更未知" in html
    assert "可见次数不能证明实际次数不足" in html
    assert "1202 LOC" not in html


@pytest.mark.parametrize("name,shallow", [("complete_root", False), ("marked_real_root", True)])
def test_genuine_initial_import_stays_known_large_positive(histories, name, shallow):
    case = histories[name]
    commits, actual_shallow = development.collect_commits(case["root"])
    assert actual_shallow is shallow
    assert commits[0]["parents"] == []
    assert commits[0]["change_metrics_basis"] == "root_empty_tree"
    assert commits[0]["change_metrics_complete"]
    assert commits[0]["additions"] == 1202 and commits[0]["deletions"] == 0
    evidence = development.build_development_evidence(commits, shallow=shallow)
    large = next(c for c in evidence["candidates"] if c["candidate_id"] == "large-commits")
    assert large["commit_shas"] == [case["sha"]]


def test_resolved_merge_is_unknown_not_zero_or_author_effort(histories):
    case = histories["resolved_merge"]
    assert known_numstat(case["root"], case["left"], case["merge"]) == 2
    # A local config can change Git's default; collector explicitly fixes its basis.
    git(case["root"], "config", "log.diffMerges", "first-parent")
    commits, shallow = development.collect_commits(case["root"])
    merge = next(c for c in commits if c["sha"] == case["merge"])
    assert not shallow
    assert merge["parents"] == [case["left"], case["right"]]
    assert merge["change_metrics_basis"] == "merge_numstat_unmeasured"
    assert merge["additions"] is None and merge["deletions"] is None
    assert merge["numstat_observation"] == {"additions": 0, "deletions": 0, "files": []}
    evidence = development.build_development_evidence(commits)
    analysis = development.analyze_history("merge", commits, neutral_ai(commits, evidence))
    assert analysis["stages"][0]["key_commits"][0]["loc"] is None
    html = development.render_development_html(analysis)
    assert "Resolve source conflict · 变更未知" in html
    assert "Resolve source conflict · 0 LOC" not in html
    assert "1 次提交变更未知" in analysis["digest"].modules[0].summary


def test_all_unknown_stage_has_no_false_zero_total(histories):
    case = histories["resolved_merge"]
    commits, _ = development.collect_commits(case["root"])
    merge = next(c for c in commits if c["sha"] == case["merge"])
    evidence = development.build_development_evidence([merge])
    analysis = development.analyze_history("merge-only", [merge], neutral_ai([merge], evidence))
    html = development.render_development_html(analysis)
    assert "变更量未知（1 次提交）" in html
    assert "变更 0 LOC" not in html
    assert "变更量未知" in analysis["digest"].modules[0].summary


def test_author_dates_do_not_reorder_commits_or_invert_stage_range(histories):
    case = histories["nonmonotone_dates"]
    commits, _ = development.collect_commits(case["root"])
    assert [c["sha"] for c in commits] == [case["first"], case["second"]]
    assert datetime.fromisoformat(commits[0]["author_date"]) > datetime.fromisoformat(commits[1]["author_date"])
    assert datetime.fromisoformat(commits[0]["committer_date"]) < datetime.fromisoformat(commits[1]["committer_date"])
    evidence = development.build_development_evidence(commits)
    analysis = development.analyze_history("dates", commits, neutral_ai(commits, evidence))
    stage = analysis["stages"][0]
    assert (stage["start"], stage["end"]) == ("2026-10-02", "2026-10-07")
    assert analysis["digest"].metrics["start_date"] == "2026-10-02"
    assert analysis["digest"].metrics["end_date"] == "2026-10-07"
    assert evidence["timeline"][1]["committer_date"] == commits[1]["committer_date"]
    assert evidence["timeline"][1]["parents"] == [case["first"]]
    html = development.render_development_html(analysis)
    assert "作者声明日期范围（UTC），不是实际工作起止时间" in html
    assert "2026-10-07 至 2026-10-02" not in html


def test_missing_shallow_manifest_still_checks_raw_parent(histories, monkeypatch):
    case = histories["shallow_boundary"]
    actual = development._git

    def inaccessible_manifest(repo, *args):
        if args == ("rev-parse", "--git-path", "shallow"):
            raise RuntimeError("manifest access failed")
        return actual(repo, *args)

    monkeypatch.setattr(development, "_git", inaccessible_manifest)
    commits, _ = development.collect_commits(case["root"])
    assert commits[0]["parents"] == [case["parent"]]
    assert commits[0]["change_metrics_basis"] == "shallow_boundary_unresolved"


def test_failed_original_parent_check_preserves_unknown(histories, monkeypatch):
    case = histories["shallow_boundary"]
    actual = development._git

    def unavailable_object(repo, *args):
        if args == ("cat-file", "-p", case["boundary"]):
            raise RuntimeError("object unavailable")
        return actual(repo, *args)

    monkeypatch.setattr(development, "_git", unavailable_object)
    commits, _ = development.collect_commits(case["root"])
    assert commits[0]["change_metrics_basis"] == "shallow_boundary_unresolved"
    assert commits[0]["additions"] is None
