"""GitLab 文件跳转链接构建。

把查重报告里的 文件:行 引用渲染成可点击的 GitLab blob 链接
（`https://<host>/<namespace>/<project>/-/blob/<sha>/<path>#L<start>-L<end>`），
取代 vscode:// 本地跳转。

  - repo_id ("2025/AstranciA") → repo_url：从 config/repos.yaml 按 (year, team_name) 查。
  - repo_url → HEAD sha：`git ls-remote <url> HEAD` 匿名获取，缓存到 data/db/repo_heads.json。
    sha 是稳定 permalink（除非强制推送），避免依赖不确定的默认分支名。
  - 新作品（query）的 repo_url/sha：从本地克隆 `git remote get-url origin` + `rev-parse HEAD`。
"""

from __future__ import annotations

import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from loguru import logger

DEFAULT_HEADS_CACHE = "data/db/repo_heads.json"
_LS_REMOTE_TIMEOUT = 15  # 单仓 ls-remote 超时秒数
_FETCH_WORKERS = 8


def build_repo_url_map(repos_yaml: str | Path = "config/repos.yaml") -> dict[str, str]:
    """repo_id ("year/team_name") → repo_url。从 repos.yaml 读。"""
    from src.ingest.config import load_repos

    m: dict[str, str] = {}
    try:
        for e in load_repos(repos_yaml):
            key = f"{e.year}/{e.team_name}"
            if e.repo_url:
                m[key] = e.repo_url
    except Exception as e:  # noqa: BLE001
        logger.warning("读取 {} 建 repo_url 映射失败：{}", repos_yaml, e)
    return m


def _canonical_repo_url(url: str) -> str:
    """去掉 .git 后缀与尾斜杠，作为缓存键。"""
    u = (url or "").strip()
    if u.endswith(".git"):
        u = u[:-4]
    return u.rstrip("/")


def _parse_repo_url(url: str) -> tuple[str, str] | None:
    """repo_url → (host, namespace/project)。"""
    p = urlsplit(url)
    if not p.scheme or not p.netloc:
        return None
    path = p.path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not path:
        return None
    return p.netloc, path


def fetch_head(repo_url: str) -> str | None:
    """`git ls-remote <url> HEAD` → sha（匿名，超时保护）。失败返回 None。"""
    try:
        r = subprocess.run(
            ["git", "ls-remote", repo_url, "HEAD"],
            capture_output=True, timeout=_LS_REMOTE_TIMEOUT,
            env={**__import__("os").environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        if r.returncode != 0 or not r.stdout:
            return None
        first = r.stdout.decode("utf-8", errors="replace").splitlines()[0].strip()
        if not first:
            return None
        sha = first.split("\t", 1)[0]
        return sha if re.fullmatch(r"[0-9a-f]{40}", sha) else None
    except Exception:  # noqa: BLE001 — 超时/网络错误
        return None


def load_heads(cache_path: str | Path = DEFAULT_HEADS_CACHE) -> dict[str, str]:
    p = Path(cache_path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_heads(heads: dict[str, str], cache_path: str | Path = DEFAULT_HEADS_CACHE) -> None:
    p = Path(cache_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(heads, ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_heads(
    repo_urls: list[str],
    cache_path: str | Path = DEFAULT_HEADS_CACHE,
    *,
    workers: int = _FETCH_WORKERS,
) -> dict[str, str]:
    """补全 repo_url → sha 缓存（仅对缺失的发 ls-remote），返回完整 dict。"""
    heads = load_heads(cache_path)
    canon_to_orig = {_canonical_repo_url(u): u for u in repo_urls}
    missing = [orig for canon, orig in canon_to_orig.items() if canon not in heads]
    if not missing:
        return heads

    logger.info("[gitlab-heads] 首次获取 {} 个仓库 HEAD（并发 {}，每仓超时 {}s）…",
                len(missing), workers, _LS_REMOTE_TIMEOUT)
    ok = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_head, u): u for u in missing}
        for fut in as_completed(futs):
            u = futs[fut]
            try:
                sha = fut.result()
            except Exception:  # noqa: BLE001
                sha = None
            if sha:
                heads[_canonical_repo_url(u)] = sha
                ok += 1
    save_heads(heads, cache_path)
    logger.info("[gitlab-heads] 完成：成功 {}/{}，缓存 → {}", ok, len(missing), cache_path)
    return heads


def gitlab_blob_url(
    repo_url: str, sha: str | None, file_path: str, start: int, end: int,
) -> str | None:
    """构造 GitLab blob 永久链接。无 sha 时回退到 master 分支（不保证可达）。"""
    parsed = _parse_repo_url(repo_url)
    if not parsed:
        return None
    host, nsp = parsed
    # 归一化文件路径（Windows 反斜杠 → 正斜杠，去前导 ./）
    fp = (file_path or "").replace("\\", "/").lstrip("./")
    if not fp:
        return None
    ref = sha or "master"
    # 行锚：单行 #L5，多行 #L5-10
    if start and end and end > start:
        anchor = f"#L{start}-{end}"
    elif start:
        anchor = f"#L{start}"
    else:
        anchor = ""
    return f"https://{host}/{nsp}/-/blob/{ref}/{fp}{anchor}"


def query_repo_info(local_path: str | Path) -> tuple[str | None, str | None]:
    """从本地 git 克隆取 (remote_url, HEAD sha)。非 git 目录返回 (None, None)。"""
    path = Path(local_path)
    if not path.exists():
        return None, None

    def _git(*args: str) -> str | None:
        try:
            r = subprocess.run(
                ["git", "-C", str(path), *args],
                capture_output=True, timeout=10,
            )
            return r.stdout.decode("utf-8", errors="replace").strip() if r.returncode == 0 else None
        except Exception:  # noqa: BLE001
            return None

    url = _git("remote", "get-url", "origin") or _git("remote", "get-url", "--all")
    sha = _git("rev-parse", "HEAD")
    return (url or None), (sha or None)


class GitLabLinker:
    """把 (repo_id, file_path, 行号) 渲染成 Markdown 链接 `[ref](url)`，无 url 时回退纯文本。"""

    def __init__(
        self,
        url_map: dict[str, str],
        heads: dict[str, str],
        *,
        query_repo_url: str | None = None,
        query_sha: str | None = None,
    ) -> None:
        self.url_map = url_map
        self.heads = heads
        self.query_repo_url = query_repo_url
        self.query_sha = query_sha

    def _resolve(self, repo_id: str) -> tuple[str | None, str | None]:
        """repo_id → (repo_url, sha)。query 仓库优先用 query_repo_url/sha。"""
        # query 仓库：repo_id 通常是 "year/team" 或 recall.query_repo_id；用注入的新作品信息
        if self.query_repo_url and repo_id and repo_id == self._query_key():
            return self.query_repo_url, self.query_sha
        url = self.url_map.get(repo_id or "")
        if not url:
            return None, None
        return url, self.heads.get(_canonical_repo_url(url))

    _qkey: str | None = None

    def _query_key(self) -> str:
        # 占位：query 仓库的 repo_id 由调用方通过 mark_query_repo 设置
        return self._qkey or ""

    def mark_query_repo(self, repo_id: str) -> None:
        """登记新作品的 repo_id，使 _resolve 命中 query_repo_url/sha。"""
        self._qkey = repo_id

    def link(self, repo_id: str | None, file_path: str, start: int, end: int,
             *, text: str | None = None) -> str:
        """返回 `[text](url)` 或纯文本 ref。text 缺省时用 `file_path:start-end`。"""
        if not text:
            if start and end and end > start:
                text = f"{file_path}:{start}-{end}"
            elif start:
                text = f"{file_path}:{start}"
            else:
                text = file_path
        if not repo_id:
            return text
        url, sha = self._resolve(repo_id)
        if not url:
            return text
        blob = gitlab_blob_url(url, sha, file_path, start, end)
        if not blob:
            return text
        # 转义 Markdown 链接文本里的 ] / ( 以免破坏链接语法
        safe_text = text.replace("]", "\\]").replace("(", "\\(").replace(")", "\\)")
        return f"[{safe_text}]({blob})"
