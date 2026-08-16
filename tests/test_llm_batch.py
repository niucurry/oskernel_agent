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


def test_opencode_session_uses_project_private_config(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    source_data = project_root / "data" / "opencode" / "data" / "opencode"
    source_config = project_root / "data" / "opencode" / "config" / "opencode"
    source_data.mkdir(parents=True)
    source_config.mkdir(parents=True)
    (source_data / "auth.json").write_text('{"deepseek": {}}', encoding="utf-8")
    (source_config / "opencode.json").write_text(
        '{"model": "deepseek/test"}', encoding="utf-8"
    )

    monkeypatch.setattr(llm_batch, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(llm_batch, "_OPENCODE_DATA_ROOT", tmp_path / "sessions")
    task = llm_batch.BatchTask(
        batch_id="private-config",
        agent_name="test",
        user_request="test",
        output_path=tmp_path / "result.json",
        cache_dir=tmp_path / "cache",
        cache_key="",
    )

    env = llm_batch._opencode_env(task)
    config_home = Path(env["XDG_CONFIG_HOME"])
    assert env["XDG_DATA_HOME"] == str(config_home.parent / "data")
    assert (config_home / "opencode" / "opencode.json").is_file()
    assert (config_home.parent / "data" / "opencode" / "auth.json").is_file()


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


def test_informed_retry_carries_delivery_failure_reason(tmp_path, monkeypatch):
    """校验器返回 str 失败原因时，重试请求携带原因，模型可针对性修正。"""
    requests = []

    def fake_run(task, timeout):
        requests.append(task.user_request)
        task.output_path.write_text(
            json.dumps({"summary": "完整结果"}), encoding="utf-8"
        )
        return True, "", []

    monkeypatch.setattr(llm_batch, "_run_opencode_once", fake_run)
    task = _batch_task(tmp_path)
    task.cache_enabled = False
    task.cache_validator = lambda value: (
        "severity 与来源不符：comparison#2" if len(requests) == 1 else True
    )

    result = llm_batch.run_batch_task(task)

    assert len(requests) == 2
    assert "【上次输出未通过交付校验】" in requests[1]
    assert "severity 与来源不符：comparison#2" in requests[1]
    assert result["summary"] == "完整结果"


def test_str_validator_means_failure_not_success(tmp_path, monkeypatch):
    """校验器返回错误字符串必须视为失败并重试，不能误当作通过。"""
    calls = {"n": 0}

    def fake_run(task, timeout):
        calls["n"] += 1
        task.output_path.write_text(
            json.dumps({"summary": "完整" if calls["n"] == 2 else ""}),
            encoding="utf-8",
        )
        return True, "", []

    monkeypatch.setattr(llm_batch, "_run_opencode_once", fake_run)
    task = _batch_task(tmp_path)
    task.cache_enabled = False
    task.cache_validator = lambda value: "每次都不行" if calls["n"] < 2 else True

    result = llm_batch.run_batch_task(task)

    assert calls["n"] == 2
    assert result["summary"] == "完整"


def test_parse_failure_retry_includes_previous_output_tail(tmp_path, monkeypatch):
    """解析失败的重试同样知情：附带上次输出片段供模型对照修正格式。"""
    requests = []

    def fake_run(task, timeout):
        requests.append(task.user_request)
        if len(requests) == 1:
            task.output_path.write_text("{broken json", encoding="utf-8")
        else:
            task.output_path.write_text(
                json.dumps({"summary": "ok"}), encoding="utf-8"
            )
        return True, "", []

    monkeypatch.setattr(llm_batch, "_run_opencode_once", fake_run)
    task = _batch_task(tmp_path)
    task.cache_enabled = False

    result = llm_batch.run_batch_task(task)

    assert len(requests) == 2
    assert "【上次输出未通过交付校验】" in requests[1]
    assert "上次输出片段" in requests[1]
    assert "{broken json" in requests[1]
    assert result["summary"] == "ok"


def test_locate_parsed_json_materializes_recovered_content_to_output_path(tmp_path):
    """别名 JSON 回收时内容回写 canonical 路径，下游直读 output_path 不落空。"""
    alias = tmp_path / ".result.json"
    content = '{"summary": "别名落盘"}'
    alias.write_text(content, encoding="utf-8")
    task = _batch_task(tmp_path)

    parsed = llm_batch._locate_parsed_json(task, ok=False, written=[alias])

    assert parsed == {"summary": "别名落盘"}
    assert task.output_path.read_text(encoding="utf-8") == content


def test_json_repair_input_prefers_written_file_over_stdout(tmp_path, monkeypatch):
    """内容写在别名路径、JSON 损坏时，修复输入必须是落盘内容而非 stdout 摘要。

    以 stdout 为输入会让修复 agent 从摘要重建结构（推倒重来）；落盘内容
    只需修语法。这里断言 _try_repair_json 收到的 raw 就是别名文件内容。
    """
    alias = tmp_path / ".result.json"
    alias.write_text('{"summary": "缺" 尾}', encoding="utf-8")
    captured = {}

    def fake_run(task, timeout):
        return False, "stdout 摘要：模型声称已生成结果", [alias]

    def fake_repair(task, raw_text, schema_hint, timeout, repair_validator=None):
        captured["raw"] = raw_text
        return {"summary": "修复后"}

    monkeypatch.setattr(llm_batch, "_run_opencode_once", fake_run)
    monkeypatch.setattr(llm_batch, "_try_repair_json", fake_repair)
    task = _batch_task(tmp_path)
    task.cache_enabled = False

    result = llm_batch.run_batch_task(task, schema_hint='{"summary":str}')

    assert captured["raw"] == '{"summary": "缺" 尾}'
    assert result == {"summary": "修复后"}


def test_try_repair_json_recovers_aliased_repair_output(tmp_path, monkeypatch):
    """修复 agent 把产物写到别名路径时回收，而不是丢弃后重跑原始任务。"""
    alias = tmp_path / "report.json"
    alias.write_text(
        json.dumps({"summary": "修复产物"}), encoding="utf-8"
    )

    def fake_run(task, timeout):
        assert task.agent_name == "os-kernel-json-repair"
        return False, "", [alias]

    monkeypatch.setattr(llm_batch, "_run_opencode_once", fake_run)
    task = _batch_task(tmp_path)

    repaired = llm_batch._try_repair_json(
        task, '{"summary": "原始损坏文本"}', '{"summary":str}', 60,
    )

    assert repaired == {"summary": "修复产物"}


def test_empty_spin_skips_json_repair_and_retries(tmp_path, monkeypatch):
    """模型空转（纯聊天、从不调用 write_report）时跳过 json_repair，直接知情重试。

    空转流程从「初始 → json_repair → 重试」3 次 LLM 调用降为 2 次；
    repair 不该被调用（没有可修的内容）。
    """
    calls = []

    def fake_run(task, timeout):
        calls.append(task.user_request)
        return False, "Let me begin. I'll make the calls.", []

    def repair_boom(*_args, **_kwargs):
        raise AssertionError("空转输出不应触发 json_repair")

    monkeypatch.setattr(llm_batch, "_run_opencode_once", fake_run)
    monkeypatch.setattr(llm_batch, "_try_repair_json", repair_boom)
    task = _batch_task(tmp_path)
    task.cache_enabled = False

    result = llm_batch.run_batch_task(task, schema_hint='{"summary":str}')

    assert len(calls) == 2                     # 初始 + 知情重试
    assert "未调用 write_report" in calls[1]   # 重试 prompt 显式提示空转
    assert result.get("_error") == "llm_batch_failed"


def test_stdout_json_mention_still_triggers_repair(tmp_path, monkeypatch):
    """stdout 声明写盘（report.json）时仍触发 json_repair，保守门不误伤。"""
    captured = []

    def fake_run(task, timeout):
        return False, "I wrote the result to report.json", []

    def fake_repair(task, raw_text, schema_hint, timeout, repair_validator=None):
        captured.append(raw_text)
        return {"summary": "修复后"}

    monkeypatch.setattr(llm_batch, "_run_opencode_once", fake_run)
    monkeypatch.setattr(llm_batch, "_try_repair_json", fake_repair)
    task = _batch_task(tmp_path)
    task.cache_enabled = False

    result = llm_batch.run_batch_task(task, schema_hint='{"summary":str}')

    assert captured                     # repair 被调用
    assert result == {"summary": "修复后"}
