"""git 克隆相关工具：URL 解析、令牌注入、断点续传式克隆。"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_WINDOWS_INVALID_CHARS = re.compile(r'[<>:"\\|?*]')
_WINDOWS_PATH_MAP = ".codex_windows_path_map.json"


def _remove_tree(path: Path) -> None:
    """删除单个明确仓库目录；Windows 下先解除 Git pack 的只读属性。"""
    def _onerror(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, onerror=_onerror)


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
    """仓库是否有可用 HEAD 且工作区完整。

    不能只判断 ``.git`` 是否存在：clone/checkout 中断时 Git 会先创建该目录，旧实现会
    将这种半成品永久当成成功仓库，随后历史库静默缺仓。
    """
    dest = Path(dest)
    git_dir = dest / ".git"
    if not git_dir.exists():
        return False
    base = ["git", f"--git-dir={git_dir}", f"--work-tree={dest}"]
    try:
        subprocess.run(
            [*base, "rev-parse", "--verify", "HEAD^{commit}"],
            check=True, capture_output=True,
        )
        # 含 Windows 保留名的仓库由 git archive 安全展开，索引刻意不 checkout；映射清单
        # 即完整展开标记。HEAD 有效 + 清单存在即可，不能再用 git diff 检查空索引。
        if (dest / _WINDOWS_PATH_MAP).exists():
            return any(p.name not in (".git", _WINDOWS_PATH_MAP) for p in dest.iterdir())
        # checkout 失败通常会把索引/工作区留成大批 D；两侧都必须与 HEAD 一致。
        subprocess.run([*base, "diff", "--quiet", "HEAD", "--"],
                       check=True, capture_output=True)
        subprocess.run([*base, "diff", "--cached", "--quiet", "HEAD", "--"],
                       check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        return False
    return any(p.name != ".git" for p in dest.iterdir())


def _is_windows_unsafe_path(path: str) -> bool:
    """Git 路径是否包含 Windows 无法落盘的组件（例如 ``aux.rs``）。"""
    for part in path.replace("\\", "/").split("/"):
        stem = part.rstrip(" .").split(".", 1)[0].upper()
        if (not part or part != part.rstrip(" .") or stem in _WINDOWS_RESERVED
                or _WINDOWS_INVALID_CHARS.search(part)):
            return True
    return False


def _safe_windows_path(path: str) -> str:
    """把 Windows 保留路径映射为可落盘路径；原路径记录在映射清单中。"""
    out: list[str] = []
    for part in path.replace("\\", "/").split("/"):
        cleaned = _WINDOWS_INVALID_CHARS.sub("_", part).rstrip(" .") or "_"
        stem = cleaned.split(".", 1)[0].upper()
        if stem in _WINDOWS_RESERVED:
            cleaned = f"__win_reserved_{cleaned}"
        out.append(cleaned)
    return "/".join(out)


def _checkout_clone(dest: Path) -> None:
    """检出克隆；Windows 上安全处理仓库中的保留文件名。"""
    if os.name != "nt":
        subprocess.run(["git", "-C", str(dest), "checkout", "--force", "HEAD"],
                       check=True, capture_output=True)
        return

    raw = subprocess.run(
        ["git", "-C", str(dest), "ls-tree", "-r", "-z", "--name-only", "HEAD"],
        check=True, capture_output=True,
    ).stdout
    paths = [p.decode("utf-8", "surrogateescape") for p in raw.split(b"\0") if p]
    unsafe = [p for p in paths if _is_windows_unsafe_path(p)]
    if not unsafe:
        subprocess.run(["git", "-C", str(dest), "checkout", "--force", "HEAD"],
                       check=True, capture_output=True)
        return

    # Git for Windows 在 sparse-checkout / archive 阶段仍会先拒绝 AUX/CON 等路径。
    # 直接按 tree OID 走 cat-file --batch 流式展开，并只改写 Windows 不可表示的组件。
    path_map: dict[str, str] = {}
    tree_raw = subprocess.run(
        ["git", "-C", str(dest), "ls-tree", "-r", "-z", "HEAD"],
        check=True, capture_output=True,
    ).stdout
    entries: list[tuple[str, str, str, str]] = []
    for item in tree_raw.split(b"\0"):
        if not item:
            continue
        meta, raw_path = item.split(b"\t", 1)
        mode, obj_type, oid = meta.decode("ascii").split()
        entries.append((mode, obj_type, oid, raw_path.decode("utf-8", "surrogateescape")))

    proc = subprocess.Popen(
        ["git", "-C", str(dest), "cat-file", "--batch"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert proc.stdin is not None and proc.stdout is not None
    try:
        for _mode, obj_type, oid, original in entries:
            if obj_type != "blob":  # 子模块 commit 等不展开
                continue
            proc.stdin.write((oid + "\n").encode("ascii"))
            proc.stdin.flush()
            header = proc.stdout.readline().decode("ascii", "replace").strip().split()
            if len(header) != 3 or header[1] != "blob":
                raise RuntimeError(f"无法读取 Git blob：{original} ({' '.join(header)})")
            size = int(header[2])
            blob = proc.stdout.read(size)
            proc.stdout.read(1)  # batch 响应末尾换行
            safe_rel = _safe_windows_path(original) if _is_windows_unsafe_path(original) else original
            if safe_rel != original:
                path_map[safe_rel] = original
            target = dest / Path(safe_rel)
            try:
                target.resolve().relative_to(dest.resolve())
            except ValueError as exc:
                raise RuntimeError(f"仓库包含越界路径：{original}") from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)
        proc.stdin.close()
        stderr = proc.stderr.read() if proc.stderr is not None else b""
        if proc.wait() != 0:
            raise subprocess.CalledProcessError(proc.returncode, proc.args, stderr=stderr)
    except Exception:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        raise
    (dest / _WINDOWS_PATH_MAP).write_text(
        json.dumps(path_map, ensure_ascii=False, indent=2), encoding="utf-8")


def clone_repo(
    repo_url: str,
    dest: str | Path,
    *,
    token: str | None = None,
    force: bool = False,
    depth: int | None = None,
    branch: str | None = None,
) -> str:
    """克隆仓库到 dest，支持断点续传与指定分支。

    返回状态："skipped"（已存在且未 --force）/ "recloned"（--force 重克隆）/ "cloned"。
    """
    dest = Path(dest)
    if is_cloned(dest):
        if not force:
            return "skipped"
        _remove_tree(dest)
        status = "recloned"
    else:
        had_incomplete = dest.exists()
        if had_incomplete:  # 残留的不完整目录
            _remove_tree(dest)
        status = "recovered" if had_incomplete else "cloned"

    dest.parent.mkdir(parents=True, exist_ok=True)
    # 先只克隆对象，再由 _checkout_clone 统一检出。这样 Windows 遇到 aux.rs 等保留名时
    # 不会留下只有 .git/objects 的半成品仓库。
    cmd = ["git", "clone", "--no-checkout"]
    if depth:
        cmd += ["--depth", str(depth)]
    if branch:
        cmd += ["--branch", branch]
    cmd += [_auth_url(repo_url, token), str(dest)]
    # 不回显带 token 的 URL
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        if os.name == "nt":
            # Windows 默认 260 字符路径上限：存储键加深层目录会把长路径仓库的
            # checkout 卡成 "Filename too long"；Git for Windows 用 core.longpaths 解除。
            # 写入仓库本地配置，后续 ls-tree / checkout / diff 一并生效。
            subprocess.run(
                ["git", "-C", str(dest), "config", "core.longpaths", "true"],
                check=True, capture_output=True,
            )
        _checkout_clone(dest)
    except Exception:
        # 失败目录绝不能留给下一次 is_cloned() 误判；克隆可安全重试。
        if dest.exists():
            _remove_tree(dest)
        raise
    return status
