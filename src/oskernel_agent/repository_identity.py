"""项目级仓库身份契约：把远端地址映射为稳定、路径安全的存储键。"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import unquote, urlsplit


_SCP_URL = re.compile(
    r"^(?:(?P<user>[^@/:]+)@)?(?P<host>[^/:]+):(?P<path>.+)$"
)
_UNSAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _normalize_repo_path(path: str) -> str:
    normalized = re.sub(r"/+", "/", path.strip()).strip("/")
    if normalized.casefold().endswith(".git"):
        normalized = normalized[:-4]
    return normalized.rstrip("/")


def canonical_repository_identity(repo_url: str) -> str:
    """返回不含凭据、查询参数和协议差异的稳定远端身份。"""
    value = repo_url.strip()
    if not value:
        raise ValueError("仓库地址不能为空")

    if "://" not in value:
        scp = _SCP_URL.fullmatch(value)
        if scp:
            host = scp.group("host").casefold()
            raw_path = scp.group("path").split("#", 1)[0].split("?", 1)[0]
            path = _normalize_repo_path(raw_path)
            if not path:
                raise ValueError(f"仓库地址缺少项目路径：{repo_url!r}")
            return f"{host}/{path}"

    parsed = urlsplit(value)
    if parsed.hostname:
        host = parsed.hostname.casefold()
        if parsed.port:
            host = f"{host}:{parsed.port}"
        path = _normalize_repo_path(parsed.path)
        if not path:
            raise ValueError(f"仓库地址缺少项目路径：{repo_url!r}")
        return f"{host}/{path}"

    fallback = _normalize_repo_path(value)
    if not fallback:
        raise ValueError(f"仓库地址缺少项目路径：{repo_url!r}")
    return fallback


def repository_display_name(repo_url: str) -> str:
    """返回适合日志展示的仓库名，不作为唯一身份使用。"""
    identity = canonical_repository_identity(repo_url)
    return unquote(identity.rsplit("/", 1)[-1])


def repository_storage_key(repo_url: str) -> str:
    """返回可作为单个目录名的稳定、防碰撞仓库存储键。"""
    identity = canonical_repository_identity(repo_url)
    display_name = repository_display_name(repo_url)
    slug = _UNSAFE_COMPONENT.sub("-", display_name).strip(" .-_").casefold()
    slug = slug[:48].rstrip(" .-_") or "repository"
    if slug.upper() in _WINDOWS_RESERVED:
        slug = f"repository-{slug}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"{slug}-{digest}"
