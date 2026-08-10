from __future__ import annotations

from pathlib import Path

from oskernel_agent.cli import batch as run_batch
from oskernel_agent.cli import fetch_repo as fetch_repo_module
from oskernel_agent.repository_identity import repository_storage_key
from oskernel_agent.comparison.pipeline import steps


SAME_NAME_URLS = (
    "https://git.example/org-a/kernel.git",
    "https://git.example/org-b/kernel.git",
)


def test_pipeline_uses_distinct_workspaces_for_same_basename_urls(
    tmp_path: Path, monkeypatch
):
    cloned_to: list[Path] = []
    monkeypatch.setattr(steps, "is_cloned", lambda _path: False)
    monkeypatch.setattr(
        steps,
        "clone_repo",
        lambda _url, dest, **_kwargs: cloned_to.append(Path(dest)),
    )

    workspaces = [steps.local_ingest(url, tmp_path) for url in SAME_NAME_URLS]

    assert workspaces[0] != workspaces[1]
    assert cloned_to == workspaces


def test_batch_uses_distinct_storage_keys_for_same_basename_urls():
    storage_keys = [run_batch.fork_to_repo_name(url) for url in SAME_NAME_URLS]

    assert storage_keys[0] != storage_keys[1]


def test_equivalent_remote_spellings_share_one_storage_key():
    expected = repository_storage_key(
        "https://token@git.example/Org/kernel.git?download=1#fragment"
    )

    assert expected == repository_storage_key("ssh://git@git.example/Org/kernel/")
    assert expected == repository_storage_key(
        "git@git.example:Org/kernel.git?download=1#fragment"
    )


def test_agent_fetch_uses_distinct_workspaces_for_same_basename_urls(
    tmp_path: Path, monkeypatch
):
    cloned_to: list[Path] = []
    monkeypatch.setattr(fetch_repo_module.os.path, "exists", lambda _path: False)
    monkeypatch.setattr(
        fetch_repo_module.Repo,
        "clone_from",
        lambda _url, dest: cloned_to.append(Path(dest)),
    )

    workspaces = [
        Path(fetch_repo_module.fetch_repo(url, str(tmp_path)))
        for url in SAME_NAME_URLS
    ]

    assert workspaces[0] != workspaces[1]
    assert cloned_to == workspaces
