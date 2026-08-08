import json
import shutil
from pathlib import Path

from src.oskernel_agent.engines import llm_batch


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
        return True, ""

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
        return True, ""

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
