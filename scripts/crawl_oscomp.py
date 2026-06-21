"""
默认数据源：
- os-kernel-winners.md：2021-2025 内核赛道部分获奖作品

输出：
    history_db/hisRepo_metadata.json
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional


USER_AGENT = "oscomp-kernel-crawler/0.1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = PROJECT_ROOT / "history_db"

SOURCES = {
    "winners": {
        "source_type": "winner",
        "source_file": "os-kernel-winners.md",
        "source_url": "https://raw.githubusercontent.com/oscomp/os-competition-info/main/os-kernel-winners.md",
        "stage": "award",
        "default_year": None,
    },
    "preliminary_2025": {
        "source_type": "preliminary_2025",
        "source_file": "20250701-kernel-comp-repos.md",
        "source_url": "https://raw.githubusercontent.com/oscomp/os-competition-info/main/20250701-kernel-comp-repos.md",
        "stage": "preliminary",
        "default_year": 2025,
    },
}


@dataclass
class KernelProject:
    year: Optional[int]
    track: str
    stage: str
    award: str
    team_id: str
    team_name: str
    school: str
    project_name: str
    repo_name: str
    repo_url: str
    clone_url: str
    repo_host: str
    source_type: str
    source_file: str
    source_url: str
    collected_at: str


def fetch_text(url: str, timeout: int = 30, retries: int = 3) -> str:
    last_error: Optional[BaseException] = None

    for i in range(retries):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/plain,*/*",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, errors="replace")
        except Exception as exc:
            last_error = exc
            if i < retries - 1:
                time.sleep(1.5 * (i + 1))

    raise RuntimeError(f"下载失败: {url}: {last_error}") from last_error


def normalize_markdown_table_text(text: str) -> str:
    """
    正常情况下 GitHub raw 会保留换行。
    这里加一个兜底：如果文本被压成一行，也尽量按 Markdown 表格行切回来。
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    if text.count("\n") <= 2 and text.count("|") > 20:
        # 把“行尾 | 下一行开头 |”修复成换行。
        text = re.sub(r"\|\s+\|", "|\n|", text)

        # 处理类似 “## title | col1 | col2 |” 的情况。
        text = re.sub(r"(#+[^\n]*?)\s+(\|\s*[^|\n]+\s*\|)", r"\1\n\2", text)

    return text


def is_separator_row(cells: list[str]) -> bool:
    if not cells:
        return False
    cleaned = [c.strip().replace(" ", "") for c in cells]
    return all(re.fullmatch(r":?-{3,}:?", c) for c in cleaned if c)


def parse_markdown_table(text: str) -> list[dict[str, str]]:
    """
    解析简单 Markdown 表格。
    返回 [{header: value, ...}, ...]
    """
    text = normalize_markdown_table_text(text)

    table_rows: list[list[str]] = []

    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue

        cells = [c.strip() for c in s.strip("|").split("|")]

        if not cells:
            continue
        if is_separator_row(cells):
            continue

        table_rows.append(cells)

    if len(table_rows) < 2:
        return []

    header = table_rows[0]
    rows: list[dict[str, str]] = []

    for cells in table_rows[1:]:
        if len(cells) < len(header):
            continue

        row = {}
        for k, v in zip(header, cells):
            row[k.strip()] = v.strip()
        rows.append(row)

    return rows


def clean_markdown_text(s: str) -> str:
    s = s.strip()

    # [text](url) -> text
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)

    # 去掉简单 HTML tag
    s = re.sub(r"<[^>]+>", "", s)

    return s.strip()


def first_url(s: str) -> str:
    """
    从单元格中抽取第一个 URL。
    支持纯 URL，也支持 Markdown 链接。
    """
    s = s.strip()
    m = re.search(r"https?://[^\s)\]>|]+", s)
    if not m:
        return ""

    url = m.group(0).strip().rstrip(".,;，。；")
    return canonical_repo_url(url)


def canonical_repo_url(url: str) -> str:
    url = url.strip().strip("<>").rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    return url


def clone_url_from_repo_url(repo_url: str) -> str:
    repo_url = canonical_repo_url(repo_url)
    return repo_url + ".git"


def repo_host(repo_url: str) -> str:
    try:
        return urllib.parse.urlparse(repo_url).netloc.lower()
    except Exception:
        return ""


def repo_name_from_url(repo_url: str) -> str:
    path = urllib.parse.urlparse(repo_url).path.rstrip("/")
    if not path:
        return ""
    return urllib.parse.unquote(path.split("/")[-1])


def parse_year(s: str) -> Optional[int]:
    m = re.search(r"(20\d{2})", s or "")
    if not m:
        return None
    return int(m.group(1))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_winner_projects(markdown: str, source: dict[str, object]) -> list[KernelProject]:
    """
    解析 os-kernel-winners.md

    表头通常是：
    年份 | 奖项 | 获奖队伍 | 获奖学校 | 作品链接
    """
    rows = parse_markdown_table(markdown)
    collected_at = now_iso()
    projects: list[KernelProject] = []

    for row in rows:
        repo_url = first_url(row.get("作品链接", ""))
        if not repo_url:
            continue

        team_name = clean_markdown_text(row.get("获奖队伍", ""))
        school = clean_markdown_text(row.get("获奖学校", ""))
        award = clean_markdown_text(row.get("奖项", ""))
        year = parse_year(row.get("年份", ""))

        repo_name = repo_name_from_url(repo_url)

        projects.append(
            KernelProject(
                year=year,
                track="kernel",
                stage=str(source["stage"]),
                award=award,
                team_id="",
                team_name=team_name,
                school=school,
                project_name=repo_name,
                repo_name=repo_name,
                repo_url=repo_url,
                clone_url=clone_url_from_repo_url(repo_url),
                repo_host=repo_host(repo_url),
                source_type=str(source["source_type"]),
                source_file=str(source["source_file"]),
                source_url=str(source["source_url"]),
                collected_at=collected_at,
            )
        )

    return projects


def parse_preliminary_2025_projects(markdown: str, source: dict[str, object]) -> list[KernelProject]:
    """
    解析 20250701-kernel-comp-repos.md

    表头通常是：
    队伍编号 | 队伍名称 | 学校 | Fork的项目
    """
    rows = parse_markdown_table(markdown)
    collected_at = now_iso()
    projects: list[KernelProject] = []

    for row in rows:
        repo_url = first_url(row.get("Fork的项目", ""))
        if not repo_url:
            continue

        team_id = clean_markdown_text(row.get("队伍编号", ""))
        team_name = clean_markdown_text(row.get("队伍名称", ""))
        school = clean_markdown_text(row.get("学校", ""))
        repo_name = repo_name_from_url(repo_url)

        projects.append(
            KernelProject(
                year=int(source["default_year"]),
                track="kernel",
                stage=str(source["stage"]),
                award="",
                team_id=team_id,
                team_name=team_name,
                school=school,
                project_name=repo_name,
                repo_name=repo_name,
                repo_url=repo_url,
                clone_url=clone_url_from_repo_url(repo_url),
                repo_host=repo_host(repo_url),
                source_type=str(source["source_type"]),
                source_file=str(source["source_file"]),
                source_url=str(source["source_url"]),
                collected_at=collected_at,
            )
        )

    return projects


def dedupe_projects(projects: Iterable[KernelProject]) -> list[KernelProject]:
    """
    按 repo_url + source_type 去重。
    这样同一个项目如果同时出现在“获奖名单”和“初赛名单”中，也可以分别保留来源。
    """
    seen: set[tuple[str, str]] = set()
    out: list[KernelProject] = []

    for p in projects:
        key = (p.repo_url, p.source_type)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)

    return out


def write_csv(projects: list[KernelProject], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [f.name for f in fields(KernelProject)]

    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for p in projects:
            writer.writerow(asdict(p))


def write_json(projects: list[KernelProject], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    data = [asdict(p) for p in projects]
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_sqlite(projects: list[KernelProject], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kernel_projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                year INTEGER,
                track TEXT NOT NULL,
                stage TEXT NOT NULL,
                award TEXT,
                team_id TEXT,
                team_name TEXT,
                school TEXT,
                project_name TEXT,
                repo_name TEXT,
                repo_url TEXT NOT NULL,
                clone_url TEXT,
                repo_host TEXT,
                source_type TEXT NOT NULL,
                source_file TEXT,
                source_url TEXT,
                collected_at TEXT,
                UNIQUE(repo_url, source_type)
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_kernel_projects_year
            ON kernel_projects(year)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_kernel_projects_team
            ON kernel_projects(team_name)
            """
        )

        columns = [f.name for f in fields(KernelProject)]
        placeholders = ", ".join(["?"] * len(columns))
        column_sql = ", ".join(columns)

        update_sql = ", ".join(
            f"{c}=excluded.{c}"
            for c in columns
            if c not in {"repo_url", "source_type"}
        )

        sql = f"""
            INSERT INTO kernel_projects ({column_sql})
            VALUES ({placeholders})
            ON CONFLICT(repo_url, source_type)
            DO UPDATE SET {update_sql}
        """

        for p in projects:
            row = asdict(p)
            conn.execute(sql, [row[c] for c in columns])

        conn.commit()
    finally:
        conn.close()


def slugify(s: str, max_len: int = 80) -> str:
    s = s.strip()
    s = re.sub(r"[\\/:*?\"<>|]+", "_", s)
    s = re.sub(r"\s+", "_", s)
    s = s.strip("._-")
    return s[:max_len] or "unknown"


def clone_projects(projects: list[KernelProject], repo_dir: Path, update: bool = False) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)

    for p in projects:
        year = str(p.year or "unknown")
        team = slugify(p.team_name)
        repo = slugify(p.repo_name)
        dirname = f"{year}_{team}_{repo}"
        target = repo_dir / dirname

        if target.exists():
            if update:
                print(f"[git pull] {target}")
                subprocess.run(
                    ["git", "-C", str(target), "pull", "--ff-only"],
                    check=False,
                )
            else:
                print(f"[skip exists] {target}")
            continue

        print(f"[git clone] {p.clone_url} -> {target}")
        subprocess.run(
            ["git", "clone", "--depth", "1", p.clone_url, str(target)],
            check=False,
        )


def crawl(include_preliminary_2025: bool) -> list[KernelProject]:
    all_projects: list[KernelProject] = []

    winners_source = SOURCES["winners"]
    winners_md = fetch_text(str(winners_source["source_url"]))
    all_projects.extend(parse_winner_projects(winners_md, winners_source))

    if include_preliminary_2025:
        prelim_source = SOURCES["preliminary_2025"]
        prelim_md = fetch_text(str(prelim_source["source_url"]))
        all_projects.extend(parse_preliminary_2025_projects(prelim_md, prelim_source))

    return dedupe_projects(all_projects)


def print_summary(projects: list[KernelProject]) -> None:
    by_source: dict[str, int] = {}
    by_year: dict[str, int] = {}

    for p in projects:
        by_source[p.source_type] = by_source.get(p.source_type, 0) + 1
        y = str(p.year or "unknown")
        by_year[y] = by_year.get(y, 0) + 1

    print()
    print(f"采集到内核赛道项目数量: {len(projects)}")

    print("\n按来源统计:")
    for k in sorted(by_source):
        print(f"  {k}: {by_source[k]}")

    print("\n按年份统计:")
    for k in sorted(by_year, reverse=True):
        print(f"  {k}: {by_year[k]}")

    print("\n前 10 条:")
    for p in projects[:10]:
        print(f"  {p.year} | {p.stage} | {p.award or '-'} | {p.team_name} | {p.school} | {p.repo_url}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="只爬 OS 比赛内核赛道项目元数据，并保存为 CSV/JSON/SQLite。"
    )

    parser.add_argument(
        "--include-2025-preliminary",
        action="store_true",
        help="额外收集 2025 初赛评审阶段的部分内核赛道开源作品。",
    )

    parser.add_argument(
        "--out-dir",
        default=str(DEFAULT_OUT_DIR),
        help="输出目录，默认 data。",
    )

    parser.add_argument(
        "--clone",
        action="store_true",
        help="可选：把仓库浅克隆到本地。默认不克隆。",
    )

    parser.add_argument(
        "--repo-dir",
        default="repos",
        help="--clone 时的仓库保存目录，默认 repos。",
    )

    parser.add_argument(
        "--update",
        action="store_true",
        help="--clone 时，如果仓库目录已存在，则执行 git pull --ff-only。",
    )

    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    csv_path = out_dir / "oscomp_kernel_projects.csv"
    json_path = out_dir / "oscomp_kernel_projects.json"
    sqlite_path = out_dir / "oscomp_kernel_projects.sqlite"

    try:
        projects = crawl(include_preliminary_2025=args.include_2025_preliminary)
    except Exception as exc:
        print(f"[ERROR] 爬取失败: {exc}", file=sys.stderr)
        return 1

    write_csv(projects, csv_path)
    write_json(projects, json_path)
    write_sqlite(projects, sqlite_path)

    print_summary(projects)

    print()
    print(f"CSV 已写入:    {csv_path}")
    print(f"JSON 已写入:   {json_path}")
    print(f"SQLite 已写入: {sqlite_path}")

    if args.clone:
        clone_projects(projects, Path(args.repo_dir), update=args.update)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
