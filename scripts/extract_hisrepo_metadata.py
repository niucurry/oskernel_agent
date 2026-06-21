"""
从 OS 比赛详情页的“优秀作品开源”表格提取历史作品 metadata。

适用页面：
  https://os.xtnl.org.cn/#/oldDetail?name=...

输出默认写到：
  history_db/hisRepo_metadata.json

说明：
  该站点是前端动态渲染页面，直接用 --url 抓取时，服务端返回的 HTML
  可能不包含表格。若 --url 没抓到数据，请在浏览器开发者工具中复制包含
  表格的 HTML，保存为本地 .html 文件后用 --input 解析。
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "history_db" / "hisRepo_metadata.json"
DEFAULT_TEMP_HTML = PROJECT_ROOT / "history_db" / "hisRepo_metadata.tmp.html"
USER_AGENT = "hisrepo-metadata-extractor/0.1"
DEFAULT_RENDER_WAIT_MS = 12000

DEFAULT_URL = "https://os.xtnl.org.cn/#/oldDetail?name=2025%E5%B9%B4%E5%85%A8%E5%9B%BD%E5%A4%A7%E5%AD%A6%E7%94%9F%E8%AE%A1%E7%AE%97%E6%9C%BA%E7%B3%BB%E7%BB%9F%E8%83%BD%E5%8A%9B%E5%A4%A7%E8%B5%9B-%E6%93%8D%E4%BD%9C%E7%B3%BB%E7%BB%9F%E8%AE%BE%E8%AE%A1%E8%B5%9B%28%E5%85%A8%E5%9B%BD%29-OS%E5%86%85%E6%A0%B8%E5%AE%9E%E7%8E%B0%E8%B5%9B%E9%81%93"

# 如果脚本自动找不到浏览器，可以把 Chrome/Edge 的 exe 路径填在这里。
# 示例：BROWSER_EXE = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
BROWSER_EXE = ""


@dataclass
class HisRepoMetadata:
    project_id: str
    year: int | None
    competition_name: str
    track: str
    stage: str
    team_name: str
    school: str
    repo_url: str
    clone_url: str
    repo_host: str
    repo_name: str
    source_page_url: str
    source_table_title: str
    collected_at: str


class TableHTMLParser(HTMLParser):
    """提取 HTML 中所有 table 的表头、单元格文本和链接。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[dict[str, object]]]] = []
        self._in_table = False
        self._in_row = False
        self._in_cell = False
        self._current_table: list[list[dict[str, object]]] = []
        self._current_row: list[dict[str, object]] = []
        self._current_cell: dict[str, object] | None = None
        self._current_href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {k.lower(): v for k, v in attrs}
        tag = tag.lower()

        if tag == "table":
            self._in_table = True
            self._current_table = []
        elif self._in_table and tag == "tr":
            self._in_row = True
            self._current_row = []
        elif self._in_row and tag in ("td", "th"):
            self._in_cell = True
            self._current_cell = {"text": "", "hrefs": []}
        elif self._in_cell and tag == "a":
            href = attrs_dict.get("href")
            if href:
                self._current_href = href
                assert self._current_cell is not None
                self._current_cell["hrefs"].append(href)

        if self._in_cell and tag in ("br", "p", "div"):
            self.handle_data("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "a":
            self._current_href = None
        elif tag in ("td", "th") and self._in_cell:
            assert self._current_cell is not None
            self._current_cell["text"] = normalize_space(str(self._current_cell["text"]))
            self._current_row.append(self._current_cell)
            self._current_cell = None
            self._in_cell = False
        elif tag == "tr" and self._in_row:
            if self._current_row:
                self._current_table.append(self._current_row)
            self._current_row = []
            self._in_row = False
        elif tag == "table" and self._in_table:
            if self._current_table:
                self.tables.append(self._current_table)
            self._current_table = []
            self._in_table = False

    def handle_data(self, data: str) -> None:
        if self._in_cell and self._current_cell is not None:
            self._current_cell["text"] = str(self._current_cell["text"]) + data


def normalize_space(s: str) -> str:
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip()
    return s


def canonical_repo_url(url: str) -> str:
    url = html.unescape(url).strip().strip("<>").rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    return url


def clone_url(repo_url: str) -> str:
    repo_url = canonical_repo_url(repo_url)
    return repo_url + ".git" if repo_url else ""


def repo_host(repo_url: str) -> str:
    try:
        return urllib.parse.urlparse(repo_url).netloc.lower()
    except Exception:
        return ""


def repo_name(repo_url: str) -> str:
    try:
        path = urllib.parse.urlparse(repo_url).path.rstrip("/")
        return urllib.parse.unquote(path.split("/")[-1]) if path else ""
    except Exception:
        return ""


def first_url(cell: dict[str, object]) -> str:
    hrefs = cell.get("hrefs") or []
    if isinstance(hrefs, list):
        for href in hrefs:
            if isinstance(href, str) and href.startswith(("http://", "https://")):
                return canonical_repo_url(href)

    text = str(cell.get("text") or "")
    m = re.search(r"https?://[^\s)>\]\"']+", text)
    return canonical_repo_url(m.group(0)) if m else ""


def fetch_url(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def find_browser_exe() -> str:
    """查找本机可用于 headless 渲染的 Chrome / Edge。"""
    if BROWSER_EXE.strip():
        return BROWSER_EXE.strip()

    for name in ("msedge", "chrome", "chromium", "google-chrome", "MicrosoftEdge"):
        hit = shutil.which(name)
        if hit:
            return hit

    candidates = [
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Users\%USERNAME%\AppData\Local\Google\Chrome\Application\chrome.exe",
    ]
    for cand in candidates:
        expanded = os.path.expandvars(cand)
        if Path(expanded).exists():
            return expanded

    return ""


def render_url_with_browser(url: str, wait_ms: int = DEFAULT_RENDER_WAIT_MS) -> str:
    """
    用本机 Chrome/Edge 的 headless 模式执行页面 JS，并输出渲染后的 DOM。

    --dump-dom 会输出最终 DOM；--virtual-time-budget 给 Vue 页面留出接口
    请求和渲染时间。
    """
    browser = find_browser_exe()
    if not browser:
        raise RuntimeError(
            "未找到 Chrome/Edge 浏览器。请安装 Chrome/Edge，或在脚本顶部填写 BROWSER_EXE。"
        )

    cmd = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--disable-extensions",
        "--no-first-run",
        "--no-default-browser-check",
        f"--virtual-time-budget={max(wait_ms, 1000)}",
        "--dump-dom",
        url,
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=max(30, wait_ms // 1000 + 20),
    )
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        raise RuntimeError(f"浏览器渲染失败，退出码 {proc.returncode}: {err[:500]}")

    html_text = proc.stdout or ""
    if "<table" not in html_text and "源码仓库地址" not in html_text:
        print(
            "[WARN] 渲染后的 HTML 中未发现表格关键词，可能需要增大 --render-wait-ms，"
            "或该页面接口需要登录态。",
            file=sys.stderr,
        )
    return html_text


def looks_like_url(value: str) -> bool:
    value = value.strip()
    return (
        value.startswith("http://")
        or value.startswith("https://")
        or value.startswith("http:\\")
        or value.startswith("https:\\")
    )


def normalize_url(value: str) -> str:
    value = value.strip()
    if value.startswith("http:\\") or value.startswith("https:\\"):
        value = value.replace("\\", "/")
    value = value.replace("http:/", "http://", 1) if value.startswith("http:/") and not value.startswith("http://") else value
    value = value.replace("https:/", "https://", 1) if value.startswith("https:/") and not value.startswith("https://") else value
    return value


def infer_year(*values: str) -> int | None:
    for value in values:
        m = re.search(r"(20\d{2})", value or "")
        if m:
            return int(m.group(1))
    return None


def infer_name_from_url(source_url: str) -> str:
    if not source_url:
        return ""
    parsed = urllib.parse.urlparse(source_url)
    fragment = parsed.fragment or ""
    # 支持 #/oldDetail?name=...
    query = urllib.parse.urlparse(fragment).query or parsed.query
    params = urllib.parse.parse_qs(query)
    name = (params.get("name") or [""])[0]
    return urllib.parse.unquote(name)


def table_to_rows(table: list[list[dict[str, object]]]) -> list[dict[str, dict[str, object]]]:
    if len(table) < 2:
        return []
    headers = [normalize_space(str(c.get("text") or "")) for c in table[0]]
    if not headers:
        return []

    rows: list[dict[str, dict[str, object]]] = []
    for cells in table[1:]:
        row: dict[str, dict[str, object]] = {}
        for header, cell in zip(headers, cells):
            row[header] = cell
        rows.append(row)
    return rows


def choose_repo_table(tables: list[list[list[dict[str, object]]]]) -> list[dict[str, dict[str, object]]]:
    """选择包含“队伍名称/学校/源码仓库地址”的表格。"""
    candidates: list[tuple[int, list[dict[str, dict[str, object]]]]] = []

    for table in tables:
        rows = table_to_rows(table)
        if not rows:
            continue
        headers = set(rows[0].keys())
        score = 0
        if "队伍名称" in headers:
            score += 3
        if "学校" in headers:
            score += 3
        if "源码仓库地址" in headers:
            score += 5
        if "作品名称" in headers:
            score += 1
        if any(first_url(cell) for row in rows for cell in row.values()):
            score += 2
        if score:
            candidates.append((score, rows))

    if not candidates:
        return []
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def make_project_id(year: int | None, team_name: str, repo_url: str) -> str:
    raw = f"{year or 'unknown'}|{team_name}|{repo_url}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    prefix = str(year or "unknown")
    return f"hisrepo_{prefix}_{digest}"


def extract_metadata(
    html_text: str,
    *,
    source_page_url: str = "",
    competition_name: str = "",
    year: int | None = None,
    track: str = "kernel",
    stage: str = "open_source",
    source_table_title: str = "优秀作品开源",
) -> list[HisRepoMetadata]:
    parser = TableHTMLParser()
    parser.feed(html_text)

    rows = choose_repo_table(parser.tables)
    if not rows:
        return []

    inferred_name = competition_name or infer_name_from_url(source_page_url)
    inferred_year = year or infer_year(inferred_name, source_page_url)
    collected_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    items: list[HisRepoMetadata] = []
    for row in rows:
        team_name = normalize_space(str(row.get("队伍名称", {}).get("text", "")))
        school = normalize_space(str(row.get("学校", {}).get("text", "")))
        repo_cell = row.get("源码仓库地址") or row.get("仓库地址") or {}
        repo_url = first_url(repo_cell)
        if not repo_url:
            continue

        items.append(
            HisRepoMetadata(
                project_id=make_project_id(inferred_year, team_name, repo_url),
                year=inferred_year,
                competition_name=inferred_name,
                track=track,
                stage=stage,
                team_name=team_name,
                school=school,
                repo_url=repo_url,
                clone_url=clone_url(repo_url),
                repo_host=repo_host(repo_url),
                repo_name=repo_name(repo_url),
                source_page_url=source_page_url,
                source_table_title=source_table_title,
                collected_at=collected_at,
            )
        )

    return items


def save_temp_html(html_text: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html_text, encoding="utf-8")


def read_input(args: argparse.Namespace) -> tuple[str, str]:
    temp_html_path = Path(args.temp_html)

    if args.input:
        if looks_like_url(args.input):
            url = normalize_url(args.input)
            html_text = (
                fetch_url(url)
                if args.no_render
                else render_url_with_browser(url, args.render_wait_ms)
            )
            save_temp_html(html_text, temp_html_path)
            print(f"临时 HTML 已保存：{temp_html_path}")
            return html_text, args.source_url or url
        p = Path(args.input)
        return p.read_text(encoding=args.encoding, errors="replace"), args.source_url or ""
    if args.url:
        url = normalize_url(args.url)
        html_text = (
            fetch_url(url)
            if args.no_render
            else render_url_with_browser(url, args.render_wait_ms)
        )
        save_temp_html(html_text, temp_html_path)
        print(f"临时 HTML 已保存：{temp_html_path}")
        return html_text, url
    if DEFAULT_URL.strip():
        url = normalize_url(DEFAULT_URL)
        html_text = (
            fetch_url(url)
            if args.no_render
            else render_url_with_browser(url, args.render_wait_ms)
        )
        save_temp_html(html_text, temp_html_path)
        print(f"临时 HTML 已保存：{temp_html_path}")
        return html_text, url
    if not sys.stdin.isatty():
        return sys.stdin.read(), args.source_url or ""
    raise SystemExit(
        "请提供 --input HTML文件、--url 页面地址，或在脚本顶部填写 DEFAULT_URL。"
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description="从 OS 比赛详情页表格提取 hisRepo metadata JSON。"
    )
    ap.add_argument("--input", "-i", help="本地 HTML 文件路径。")
    ap.add_argument(
        "--url",
        help="详情页 URL。若不传，则使用脚本顶部 DEFAULT_URL。",
    )
    ap.add_argument("--source-url", default="", help="使用 --input/stdin 时记录原始页面 URL。")
    ap.add_argument("--output", "-o", default=str(DEFAULT_OUTPUT), help="输出 JSON 路径。")
    ap.add_argument(
        "--temp-html",
        default=str(DEFAULT_TEMP_HTML),
        help="抓取 URL 后保存的临时 HTML 路径。",
    )
    ap.add_argument(
        "--render-wait-ms",
        type=int,
        default=DEFAULT_RENDER_WAIT_MS,
        help="headless 浏览器渲染等待预算，默认 12000 毫秒。",
    )
    ap.add_argument(
        "--no-render",
        action="store_true",
        help="不启用浏览器渲染，退回普通 HTTP 抓取。",
    )
    ap.add_argument("--encoding", default="utf-8", help="读取本地 HTML 的编码，默认 utf-8。")
    ap.add_argument("--competition-name", default="", help="手动指定比赛名称。默认从 URL name 参数推断。")
    ap.add_argument("--year", type=int, help="手动指定年份。默认从比赛名称或 URL 推断。")
    ap.add_argument("--track", default="kernel", help="赛道标识，默认 kernel。")
    ap.add_argument("--stage", default="open_source", help="阶段标识，默认 open_source。")
    ap.add_argument(
        "--overwrite",
        action="store_true",
        help="覆盖输出文件。默认行为是续写已有 JSON，并按 repo_url 去重。",
    )
    args = ap.parse_args()

    html_text, source_url = read_input(args)
    items = extract_metadata(
        html_text,
        source_page_url=source_url,
        competition_name=args.competition_name,
        year=args.year,
        track=args.track,
        stage=args.stage,
    )

    if not items:
        print(
            "[ERROR] 未找到包含 队伍名称/学校/源码仓库地址 的表格。"
            "如果你使用的是 --url，该页面可能是前端动态渲染，请保存渲染后的 HTML 后用 --input。",
            file=sys.stderr,
        )
        return 1

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    data = [asdict(x) for x in items]
    if not args.overwrite and out_path.exists():
        try:
            old = json.loads(out_path.read_text(encoding="utf-8"))
            if isinstance(old, list):
                by_key = {x.get("repo_url", ""): x for x in old if isinstance(x, dict)}
                for item in data:
                    by_key[item["repo_url"]] = item
                data = list(by_key.values())
        except json.JSONDecodeError:
            print(f"[WARN] 旧 JSON 解析失败，将覆盖：{out_path}", file=sys.stderr)

    out_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    mode = "覆盖写入" if args.overwrite else "续写/去重后写入"
    print(f"已提取 {len(items)} 条 metadata，{mode}：{out_path}")
    for item in items[:5]:
        print(f"  {item.year or '-'} | {item.team_name} | {item.school} | {item.repo_url}")
    if len(items) > 5:
        print(f"  ... 还有 {len(items) - 5} 条")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
