"""从「内核赛作品仓库列表.xlsx」读取被分析仓库的位置（fork 地址）。

作品表通常只有单列 fork 地址（表头如 ``fork地址``），也可能带 ``仓库地址`` 等列；
本模块按表头关键字定位 URL 列，跳过空行与非 URL 行，并为每条地址派生队号
（URL 最后一段，如 ``T2026100069910651-2494``）。
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, Field

_URL_COLUMN_KEYWORDS = ("fork", "repo", "仓库", "地址", "url")
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_GITHUB_TREE_OR_BLOB = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)"
    r"/(?P<kind>tree|blob)/(?P<branch>[^/]+)/?.*$",
    re.IGNORECASE,
)


class WorksEntry(BaseModel):
    """作品仓库列表中的一条被分析仓库记录。"""

    url: str = Field(..., description="规范化后的 fork 地址（不含尾部 .git）")
    team_id: str = Field(..., description="队号，取 fork 地址最后一段")
    row: int = Field(..., description="原表行号（含表头），用于错误定位")


def team_id_from_url(url: str) -> str:
    """从 fork 地址最后一段派生队号（如 T2026100069910651-2494）。"""
    path = urlsplit(url).path.strip("/")
    if path.casefold().endswith(".git"):
        path = path[:-4]
    slug = unquote(path.rsplit("/", 1)[-1]).strip()
    if not slug:
        raise ValueError(f"无法从地址派生队号：{url!r}")
    return slug


def clone_target(url: str) -> tuple[str, str | None] | None:
    """把作品列表中的地址解析为可克隆的仓库地址与分支。

    - GitHub 的 ``/tree/<分支>``、``/blob/<分支>/…`` 网页地址 → 仓库地址 + 分支；
    - 其余 HTTP(S) 仓库地址原样返回，分支为 None；
    - GitHub 的非仓库浏览页（如 ``/commit/``、``/pull/``）返回 None，表示无法克隆。
    """
    value = url.strip().rstrip("/")
    host = (urlsplit(value).hostname or "").casefold()
    if host != "github.com":
        return value, None
    match = _GITHUB_TREE_OR_BLOB.match(value)
    if match:
        return f"https://github.com/{match['owner']}/{match['repo']}", match["branch"]
    segments = [segment for segment in urlsplit(value).path.split("/") if segment]
    return (value, None) if len(segments) == 2 else None


def _normalize_url(text: str) -> str:
    """去掉尾部斜杠与 .git 后缀；批量步骤会自行追加 ``.git`` 克隆。"""
    value = text.rstrip("/")
    if value.casefold().endswith(".git"):
        value = value[:-4]
    return value


def _is_header_row(row: tuple[object, ...]) -> bool:
    return any(
        any(keyword in str(cell or "").strip().casefold() for keyword in _URL_COLUMN_KEYWORDS)
        for cell in row
    )


def _find_url_column(header: tuple[object, ...]) -> int:
    for index, cell in enumerate(header):
        text = str(cell or "").strip().casefold()
        if any(keyword in text for keyword in _URL_COLUMN_KEYWORDS):
            return index
    return 0


def read_works_xlsx(path: str | Path) -> list[WorksEntry]:
    """读取作品仓库列表，返回去重后的 WorksEntry 列表。

    表头行按关键字识别（fork/仓库/地址/url 等）；无表头时取第一列。
    """
    import openpyxl  # 惰性导入：仅批量下载驱动需要，避免全项目依赖

    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"作品仓库列表不存在：{path}")
    sheet = openpyxl.load_workbook(path, data_only=True, read_only=True).active
    rows = list(sheet.iter_rows(values_only=True))
    if not rows:
        return []

    column = 0
    if _is_header_row(rows[0]):
        column = _find_url_column(rows[0])
        data_rows = enumerate(rows[1:], start=2)
    else:
        data_rows = enumerate(rows, start=1)

    entries: list[WorksEntry] = []
    seen: set[str] = set()
    for row_number, row in data_rows:
        value = row[column] if len(row) > column else None
        text = str(value or "").strip()
        if not _URL_RE.match(text):
            continue
        url = _normalize_url(text)
        if url.casefold() in seen:
            continue
        seen.add(url.casefold())
        entries.append(
            WorksEntry(url=url, team_id=team_id_from_url(url), row=row_number)
        )
    return entries
