import json
import shutil
from pathlib import Path

from oskernel_agent.engines import llm_batch


def test_find_opencode_ignores_inaccessible_candidates(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    def denied(_path):
        raise PermissionError("restricted user directory")

    monkeypatch.setattr(Path, "exists", denied)
    assert llm_batch._find_opencode() == "opencode"


def test_cache_disabled_task_always_regenerates_and_does_not_write_cache(
    tmp_path, monkeypatch
):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache_file = cache_dir / "same-key.json"
    cache_file.write_text(json.dumps({"summary": "旧缓存"}), encoding="utf-8")

    def fake_run(task, timeout):
        task.output_path.write_text(
            json.dumps({"summary": "本次完整新生成"}, ensure_ascii=False),
            encoding="utf-8",
        )
        return True, "", []

    monkeypatch.setattr(llm_batch, "_run_opencode_once", fake_run)
    task = llm_batch.BatchTask(
        batch_id="fresh-description",
        agent_name="test",
        user_request="test",
        output_path=tmp_path / "result.json",
        cache_dir=cache_dir,
        cache_key="same-key",
        cache_enabled=False,
    )

    result = llm_batch.run_batch_task(task)

    assert result["summary"] == "本次完整新生成"
    assert json.loads(cache_file.read_text(encoding="utf-8"))["summary"] == "旧缓存"


def test_delivery_validator_failure_retries_once(tmp_path, monkeypatch):
    calls = 0

    def fake_run(task, timeout):
        nonlocal calls
        calls += 1
        task.output_path.write_text(
            json.dumps({"summary": "完整" if calls == 2 else ""}, ensure_ascii=False),
            encoding="utf-8",
        )
        return True, "", []

    monkeypatch.setattr(llm_batch, "_run_opencode_once", fake_run)
    task = llm_batch.BatchTask(
        batch_id="semantic-retry",
        agent_name="test",
        user_request="test",
        output_path=tmp_path / "result.json",
        cache_dir=tmp_path / "cache",
        cache_key="",
        cache_enabled=False,
        cache_validator=lambda value: bool(value.get("summary")),
    )

    result = llm_batch.run_batch_task(task)

    assert calls == 2
    assert result["summary"] == "完整"


def test_long_opencode_message_is_moved_to_attachment(tmp_path):
    task = llm_batch.BatchTask(
        batch_id="long-verdict",
        agent_name="test",
        user_request="证据" * 20_000,
        output_path=tmp_path / "result.json",
        cache_dir=tmp_path / "cache",
        cache_key="",
    )

    command = llm_batch._opencode_command(task)

    assert max(map(len, command)) < llm_batch._WINDOWS_SAFE_MESSAGE_LIMIT
    prompt_path = tmp_path / ".long-verdict.prompt.txt"
    assert prompt_path.exists()
    assert prompt_path.read_text(encoding="utf-8") == task.user_request
    assert "--file" in command
    assert str(prompt_path.resolve()) in command


def _batch_task(tmp_path, name="result.json") -> llm_batch.BatchTask:
    return llm_batch.BatchTask(
        batch_id="locate-test",
        agent_name="test",
        user_request="",
        output_path=tmp_path / name,
        cache_dir=tmp_path / "cache",
        cache_key="",
    )


def test_locate_parsed_json_recovers_aliased_agent_output(tmp_path):
    """模型把 JSON 写到别名路径（.result.json）时，output_path 缺失也能找回。"""
    alias = tmp_path / ".result.json"
    alias.write_text(json.dumps({"summary": "别名落盘"}), encoding="utf-8")
    task = _batch_task(tmp_path)
    parsed = llm_batch._locate_parsed_json(task, ok=False, written=[alias])
    assert parsed == {"summary": "别名落盘"}


def test_locate_parsed_json_prefers_output_path_and_skips_repair_artifacts(tmp_path):
    output = tmp_path / "result.json"
    output.write_text(json.dumps({"source": "output"}), encoding="utf-8")
    alias = tmp_path / ".result.json"
    alias.write_text(json.dumps({"source": "alias"}), encoding="utf-8")
    repair = tmp_path / "result.repair.json"
    repair.write_text(json.dumps({"source": "repair"}), encoding="utf-8")
    task = _batch_task(tmp_path)

    assert llm_batch._locate_parsed_json(task, ok=True, written=[alias]) == {
        "source": "output",
    }
    # ok=False 时：.repair.json 修复产物不参与回收，别名文件可用
    assert llm_batch._locate_parsed_json(task, ok=False, written=[repair, alias]) == {
        "source": "alias",
    }
    # 只有 repair 产物时返回 None
    assert llm_batch._locate_parsed_json(task, ok=False, written=[repair]) is None


def test_locate_parsed_json_skips_unparseable_written_file(tmp_path):
    alias = tmp_path / ".result.json"
    alias.write_text("{broken", encoding="utf-8")
    task = _batch_task(tmp_path)
    assert llm_batch._locate_parsed_json(task, ok=False, written=[alias]) is None


def test_materialize_stdout_writes_returns_written_paths(tmp_path):
    """伪工具调用落盘后返回实际路径，供 output_path 缺失时回收。"""
    task = _batch_task(tmp_path)
    stdout = (
        '<invoke name="write_to_file">'
        '<parameter name="output_path">.alias.json</parameter>'
        '<parameter name="content">{"ok": 1}</parameter>'
        "</invoke>"
    )
    written = llm_batch._materialize_stdout_writes(task, stdout)
    assert len(written) == 1
    assert written[0] == (tmp_path / ".alias.json").resolve()
    assert json.loads(written[0].read_text(encoding="utf-8")) == {"ok": 1}


def test_repair_validator_rejects_drifted_repair_output(tmp_path, monkeypatch):
    """repair 输出结构漂移时弃用，交由上层重试原始任务。"""

    def fake_run(task, timeout):
        task.output_path.write_text(
            json.dumps({"dimensions": [{"name": "架构", "score": 80, "reason": "r"}] * 5}),
            encoding="utf-8",
        )
        return True, "", []

    monkeypatch.setattr(llm_batch, "_run_opencode_once", fake_run)
    task = _batch_task(tmp_path)

    def reject(_parsed):
        raise ValueError("dimensions 数量不足")

    assert llm_batch._try_repair_json(
        task, "raw", '{"dimensions":[...6 items...]}', 60,
        repair_validator=reject,
    ) is None

    def accept(_parsed):
        return None

    repaired = llm_batch._try_repair_json(
        task, "raw", '{"dimensions":[...6 items...]}', 60,
        repair_validator=accept,
    )
    assert repaired is not None
