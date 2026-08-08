import json
from pathlib import Path

import pytest

from src.oskernel_agent.pipeline.tree_builder import _validate_similarity_result
from src.oskernel_agent.tools.reference_db import (
    ReferenceDatabaseError,
    ReferenceOSDatabase,
    normalize_code,
)
from src.oskernel_agent.tools.tool_dispatcher import ToolDispatcher


REFERENCE = "rcore-tutorial-v3"


def _record(body: str = "pub fn shared() -> usize { 1 }") -> dict:
    return {
        "body_normalized": normalize_code(body),
        "body_hash": "0123456789abcdef0123456789abcdef",
        "calls": [],
        "file": "src/lib.rs",
        "line_count": 1,
    }


def test_all_supported_references_have_pinned_sources():
    database = ReferenceOSDatabase("reference_db")
    specs = database._source_specs()

    assert set(database.SUPPORTED) == set(specs)
    for spec in specs.values():
        assert str(spec["repo_url"]).startswith("https://")
        revision = str(spec["revision"])
        assert len(revision) == 40
        assert all(char in "0123456789abcdef" for char in revision)


def test_valid_reference_database_loads_without_rebuild(tmp_path, monkeypatch):
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    (db_dir / f"{REFERENCE}.json").write_text(
        json.dumps({"shared": _record()}), encoding="utf-8"
    )
    database = ReferenceOSDatabase(db_dir, source_specs={})

    def unexpected_rebuild(_reference_name):
        raise AssertionError("有效指纹库不应触发重建")

    monkeypatch.setattr(database, "rebuild", unexpected_rebuild)
    assert set(database.load_or_rebuild(REFERENCE)) == {"shared"}


def test_corrupt_reference_database_is_rebuilt_from_source(tmp_path):
    source = tmp_path / "reference-source"
    source.mkdir()
    (source / "lib.rs").write_text(
        "pub fn shared() -> usize {\n    1\n}\n", encoding="utf-8"
    )
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    target = db_dir / f"{REFERENCE}.json"
    target.write_text("{broken-json", encoding="utf-8")

    database = ReferenceOSDatabase(
        db_dir,
        source_specs={REFERENCE: {"local_path": str(source)}},
    )
    rebuilt = database.load_or_rebuild(REFERENCE)

    assert "shared" in rebuilt
    assert json.loads(target.read_text(encoding="utf-8"))["shared"]["body_normalized"]
    assert not list(db_dir.glob("*.tmp"))


def test_missing_reference_database_rebuild_failure_is_explicit(tmp_path):
    database = ReferenceOSDatabase(
        tmp_path / "db",
        source_specs={REFERENCE: {"local_path": str(tmp_path / "missing")}},
    )

    with pytest.raises(ReferenceDatabaseError, match="自动重建失败"):
        database.load_or_rebuild(REFERENCE)


class _FakeReferenceDatabase:
    def __init__(self):
        self.requested = []

    def load_or_rebuild(self, reference_name):
        self.requested.append(reference_name)
        return {"shared": _record()}


class _FakeEngine:
    _func_index = {
        "shared": {
            "name": "shared",
            "body": "pub fn shared() -> usize { 1 }",
            "file": "src/lib.rs",
            "calls": [],
        }
    }


def test_dispatcher_requires_code_fingerprint_database(tmp_path):
    dispatcher = ToolDispatcher(
        _FakeEngine(), None, str(tmp_path), {"primary_lang": "rust"}, {}
    )
    database = _FakeReferenceDatabase()
    dispatcher.ref_database = database

    result = dispatcher.compare_with_reference_os(REFERENCE)

    assert database.requested == [REFERENCE]
    assert "代码指纹库" in result
    assert "函数名集合" not in result


def test_reference_os_verdict_requires_fingerprint_similarity():
    facts = {"meta": {"reference_os": REFERENCE}}
    with pytest.raises(RuntimeError, match="代码指纹比对结果"):
        _validate_similarity_result({}, facts)

    _validate_similarity_result(
        {
            "similarity": {
                "reference_os": REFERENCE,
                "overlap_pct": 42,
                "summary": "代码指纹显示部分参考实现被改造。",
            }
        },
        facts,
    )
