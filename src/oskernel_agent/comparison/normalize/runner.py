"""normalize 编排：仓库 → 切分归一化 → 写入 SQLite。"""

from __future__ import annotations

import json
from pathlib import Path

from loguru import logger

from oskernel_agent.comparison.exact.matcher import normalized_file_hash, raw_file_hash
from oskernel_agent.comparison.models import FunctionRecord

from .classify import ModuleClassifier, load_classifier
from .discovery import discover_files
from .extract import DEFAULT_MIN_LINES, extract_functions
from .keep_symbols import load_keep_symbols
from .store import DEFAULT_DB, FunctionStore

DEFAULT_REPOS_ROOT = "data/repos"
DEFAULT_MAX_LINES = 10000
_WINDOWS_PATH_MAP = ".codex_windows_path_map.json"


def derive_repo_id(repo: Path, repos_root: Path) -> str:
    """仓库标识：优先取相对 repos_root 的路径（{year}/{team}），否则取目录名。"""
    repo = repo.resolve()
    try:
        return repo.relative_to(repos_root.resolve()).as_posix()
    except ValueError:
        return repo.name


def normalize_repo(
    repo: str | Path,
    store: FunctionStore,
    *,
    repo_id: str | None = None,
    repos_root: str | Path = DEFAULT_REPOS_ROOT,
    classifier: ModuleClassifier | None = None,
    keep=None,
    min_lines: int = DEFAULT_MIN_LINES,
    max_lines: int = DEFAULT_MAX_LINES,
) -> dict:
    """归一化单个仓库并写入 store，返回统计信息。"""
    repo = Path(repo)
    repos_root = Path(repos_root)
    repo_id = repo_id or derive_repo_id(repo, repos_root)
    classifier = classifier or load_classifier()
    keep = keep if keep is not None else load_keep_symbols()

    path_map: dict[str, str] = {}
    map_path = repo / _WINDOWS_PATH_MAP
    if map_path.exists():
        try:
            path_map = json.loads(map_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("[{}] Windows 路径映射读取失败 {}: {}", repo_id, map_path, exc)

    files = discover_files(repo)
    logger.info("[{}] 发现源码文件 {} 个", repo_id, len(files))

    records: list[tuple[FunctionRecord, list[str], list[str]]] = []
    file_records: list[dict] = []
    skipped_big = 0
    for f in files:
        report_path = path_map.get(f.rel_path.replace("\\", "/"), f.rel_path)
        try:
            text = f.path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("[{}] 读取失败 {}: {}", repo_id, f.rel_path, exc)
            continue
        if text.count("\n") + 1 > max_lines:
            logger.warning("[{}] 跳过超大文件（>{} 行，疑似生成代码）: {}", repo_id, max_lines, f.rel_path)
            skipped_big += 1
            continue

        fns = extract_functions(text, f.lang, keep=keep, min_lines=min_lines)
        for fn in fns:
            module_tag = classifier.classify(
                f.rel_path,
                f.lang,
                is_macro=fn.is_macro,
                func_name=fn.func_name,
                raw_code=fn.raw_code,
            )
            rec = FunctionRecord(
                repo_id=repo_id,
                file_path=report_path,
                start_line=fn.start_line,
                end_line=fn.end_line,
                func_name=fn.func_name,
                module_tag=module_tag,
                lang=fn.lang,
                raw_code=fn.raw_code,
                normalized_code=fn.normalized_code,
            )
            records.append((rec, fn.strings, fn.feature_tokens))
        file_records.append({
            "file_path": report_path,
            "lang": f.lang,
            "line_count": text.count("\n") + 1,
            "func_count": len(fns),
            "norm_hash": normalized_file_hash(text, f.lang),
            "raw_hash": raw_file_hash(text),
        })

    store.write_repo(repo_id, records, file_records=file_records)
    dist = store.module_distribution(repo_id)
    logger.info(
        "[{}] 写入函数 {} 个（跳过超大文件 {}）；模块分布: {}",
        repo_id,
        len(records),
        skipped_big,
        dist,
    )
    return {"repo_id": repo_id, "functions": len(records), "skipped_big": skipped_big, "distribution": dist}


def iter_repos(repos_root: str | Path) -> list[Path]:
    """枚举 repos_root 下的仓库：优先 {year}/{team} 两级目录，回退到含 .git 的目录。"""
    repos_root = Path(repos_root)
    repos = [p for p in repos_root.glob("*/*") if p.is_dir()]
    if repos:
        return sorted(repos)
    return sorted(p for p in repos_root.glob("*") if (p / ".git").exists())


def normalize_all(
    repos_root: str | Path = DEFAULT_REPOS_ROOT,
    db_path: str | Path = DEFAULT_DB,
    *,
    min_lines: int = DEFAULT_MIN_LINES,
    max_lines: int = DEFAULT_MAX_LINES,
) -> list[dict]:
    """归一化 repos_root 下全部仓库。"""
    repos = iter_repos(repos_root)
    logger.info("待归一化仓库 {} 个", len(repos))
    classifier = load_classifier()
    keep = load_keep_symbols()
    results = []
    with FunctionStore(db_path) as store:
        for i, repo in enumerate(repos, 1):
            logger.info("=== ({}/{}) {} ===", i, len(repos), repo)
            results.append(
                normalize_repo(
                    repo,
                    store,
                    repos_root=repos_root,
                    classifier=classifier,
                    keep=keep,
                    min_lines=min_lines,
                    max_lines=max_lines,
                )
            )
    return results
