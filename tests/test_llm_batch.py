import json

from src.oskernel_agent.engines import llm_batch


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
