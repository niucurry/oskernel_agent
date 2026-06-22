"""git 克隆相关工具：URL 解析、令牌注入、断点续传式克隆。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def project_path_from_url(repo_url: str) -> str:
    """从仓库 URL 提取 GitLab 项目路径（path_with_namespace），去掉前导斜杠与 .git。"""
    path = urlsplit(repo_url).path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return path


def base_url_from_url(repo_url: str) -> str:
    """从仓库 URL 推断 GitLab 实例根地址（scheme://host[:port]），不含凭据。"""
    p = urlsplit(repo_url)
    netloc = p.hostname or ""
    if p.port:
        netloc += f":{p.port}"
    return urlunsplit((p.scheme, netloc, "", "", ""))


def _auth_url(repo_url: str, token: str | None) -> str:
    """把 token 以 oauth2 形式注入 URL，用于克隆私有仓库。"""
    if not token:
        return repo_url
    p = urlsplit(repo_url)
    host = p.hostname or ""
    if p.port:
        host += f":{p.port}"
    netloc = f"oauth2:{token}@{host}"
    return urlunsplit((p.scheme, netloc, p.path, p.query, p.fragment))


def is_cloned(dest: str | Path) -> bool:
    dest = Path(dest)
    return (dest / ".git").exists()


def clone_repo(
    repo_url: str,
    dest: str | Path,
    *,
    token: str | None = None,
    force: bool = False,
    depth: int | None = None,
) -> str:
    """克隆仓库到 dest，支持断点续传。

    返回状态："skipped"（已存在且未 --force）/ "recloned"（--force 重克隆）/ "cloned"。
    """
    dest = Path(dest)
    if is_cloned(dest):
        if not force:
            return "skipped"
        shutil.rmtree(dest)
        status = "recloned"
    else:
        if dest.exists():  # 残留的不完整目录
            shutil.rmtree(dest)
        status = "cloned"

    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["git", "clone"]
    if depth:
        cmd += ["--depth", str(depth)]
    cmd += [_auth_url(repo_url, token), str(dest)]
    # 不回显带 token 的 URL
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return status
