# -*- coding: utf-8 -*-
"""批量修复 reports_by_work_id/<作品目录>/ 下的最终交付报告（无需 tree.json / 中间数据）。

其中约 90 个作品的报告产自云端批次，没跑过本地修复脚本，存在两类问题：
  1. comparison.html：档位标签乱（已确认借鉴 / 疑似借鉴·待人工判定 / 待人工判定 …）
     → 直接对 HTML 过 report.label_normalize.normalize_labels（与新报告写盘同一套规则，幂等）。
  2. description.html：整章英文正文（node-content / verdict-content 块）
     → 复用 pipeline.lang_guard 的 needs_translation + LLM 翻译（deepseek-v4-flash），
       在 HTML 层按块→顶层元素分片翻译后回填。
       安全阀：译文与原文的标签名序列必须一致、正文长度不得异常缩水，否则保留原文。

幂等：已修复的报告再跑一遍不会二次改写（中文块不触发翻译；normalize 本身幂等）。
备份：首次改动前把原文件备份到 data/output/_batch/workid_fix_bak/<作品目录>/。

用法：
    python scripts/fix_workid_reports.py                     # 全部 140 个作品
    python scripts/fix_workid_reports.py T202610459999616-2917 ...   # 指定作品目录名
    python scripts/fix_workid_reports.py --scan-only        # 只扫描不改写
"""
from __future__ import annotations

import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from report.label_normalize import normalize_labels, residual_legacy  # noqa: E402
from oskernel_agent.pipeline import lang_guard  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RBW = ROOT / "reports_by_work_id"
BAK = ROOT / "data" / "output" / "_batch" / "workid_fix_bak"

MODEL = "deepseek-v4-flash"
CHUNK_LIMIT = 2800          # 单次送翻的 HTML 片段上限（字符）
_VOID = {"br", "hr", "img", "input", "meta", "link", "col", "wbr", "source"}
_TAGNAME = re.compile(r"</?([a-zA-Z0-9]+)")


# ---------------------------------------------------------------- HTML 切块

def balanced_div_blocks(html: str, cls: str) -> list[tuple[int, int, str]]:
    """找出 class 含 cls 的 <div> 的 (inner_start, inner_end, inner_html)。"""
    out = []
    for m in re.finditer(r'<div class="[^"]*\b%s\b[^"]*"[^>]*>' % re.escape(cls), html):
        i = m.end()
        depth = 1
        for t in re.finditer(r"<div\b[^>]*>|</div>", html[i:]):
            depth += 1 if t.group(0).startswith("<div") else -1
            if depth == 0:
                out.append((i, i + t.start(), html[i : i + t.start()]))
                break
    return out


def top_level_segments(inner: str) -> list[str]:
    """把块内 HTML 按顶层元素边界切成若干段（保序拼回 == 原文）。"""
    segs: list[str] = []
    pos = 0
    depth = 0
    seg_start = 0
    for t in re.finditer(r"<(/?)([a-zA-Z0-9]+)[^>]*?(/?)>", inner):
        name = t.group(2).lower()
        if name in _VOID or t.group(3) == "/":
            continue
        if t.group(1):  # 闭合
            depth = max(0, depth - 1)
            if depth == 0:
                segs.append(inner[seg_start : t.end()])
                seg_start = t.end()
        else:
            depth += 1
    if seg_start < len(inner):
        segs.append(inner[seg_start:])
    return [s for s in segs if s]


def group_chunks(segs: list[str], limit: int = CHUNK_LIMIT) -> list[str]:
    """把顶层片段按 limit 聚合成翻译单元（单段超限则独立成块）。"""
    chunks: list[str] = []
    cur = ""
    for s in segs:
        if cur and len(cur) + len(s) > limit:
            chunks.append(cur)
            cur = s
        else:
            cur += s
    if cur:
        chunks.append(cur)
    return chunks


# ---------------------------------------------------------------- 翻译安全阀

_TAGTOKEN = re.compile(r"</?[a-zA-Z][^>]*>")


def _tag_seq(s: str) -> list[str]:
    """标签名序列（区分开/闭：'a' vs 'a/'）。"""
    return [_tag_name(t) for t in _TAGTOKEN.findall(s)]


def _restore_tags(out: str, src: str) -> str:
    """标签名序列一致的前提下，把译文里每个标签整体换回原文对应标签，
    防止模型悄改属性（href/class 等）。"""
    src_tokens = _TAGTOKEN.findall(src)
    parts = _TAGTOKEN.split(out)
    if len(parts) != len(src_tokens) + 1:
        return out
    rebuilt = parts[0]
    for tok, text in zip(src_tokens, parts[1:]):
        rebuilt += tok + text
    return rebuilt


def _tag_name(tok: str) -> str:
    m = _TAGNAME.match(tok)
    return (m.group(1).lower() + ("/" if tok.startswith("</") else "")) if m else tok


def _restore_tags_by_name(out: str, src: str) -> str:
    """标签名序列不同但多重集一致（翻译语序把 <a>/<code> 挪了位置）时：
    按标签名分组、按出现次序把译文第 n 个同名标签换回原文第 n 个的完整原文，
    属性仍全部来自原文，只是允许标签随语序移动。"""
    from collections import defaultdict, deque
    src_by_name: dict[str, deque[str]] = defaultdict(deque)
    for tok in _TAGTOKEN.findall(src):
        src_by_name[_tag_name(tok)].append(tok)
    out_tokens = _TAGTOKEN.findall(out)
    parts = _TAGTOKEN.split(out)
    if len(parts) != len(out_tokens) + 1:
        return out
    rebuilt = parts[0]
    for tok, text in zip(out_tokens, parts[1:]):
        q = src_by_name.get(_tag_name(tok))
        rebuilt += (q.popleft() if q else tok) + text
    return rebuilt


def _plain_len(s: str) -> int:
    return len(re.sub(r"<[^>]+>|\s+", "", s))


_no_thinking_ok = True


def _call_llm(chunk: str, retries: int = 3) -> str | None:
    """自带翻译调用：deepseek-v4-flash 是推理模型，lang_guard 的 max_tokens=4000 会被
    思考 token 吃光导致 content 为空。这里关思考、放宽 max_tokens、校验 finish_reason。"""
    import time
    global _no_thinking_ok
    cli = lang_guard._get_client()
    if cli is None:
        return None
    last = None
    for i in range(1, retries + 1):
        try:
            kw = {}
            if _no_thinking_ok:
                kw["extra_body"] = {"enable_thinking": False}
            r = cli.chat.completions.create(
                model=MODEL,
                messages=[{"role": "system", "content": lang_guard._SYS_PROMPT},
                          {"role": "user", "content": chunk}],
                temperature=0.2, max_tokens=7000, **kw)
            if r.choices[0].finish_reason != "stop":
                last = f"finish={r.choices[0].finish_reason}"
                continue
            out = lang_guard._FENCE.sub("", (r.choices[0].message.content or "").strip()).strip()
            if out:
                return out
            last = "empty content"
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if _no_thinking_ok and "enable_thinking" in msg:
                _no_thinking_ok = False   # 端点不认该参数 → 摘掉重试
                continue
            last = msg[:160]
            time.sleep(min(3 * i, 15))
    print(f"[fix] 翻译失败（{retries} 次）：{last}", file=sys.stderr, flush=True)
    return None


def safe_translate(chunk: str) -> str:
    out = _call_llm(chunk)
    if not out or out == chunk:
        return chunk
    if _plain_len(out) < _plain_len(chunk) * 0.15:  # 正文异常缩水 → 弃用
        return chunk
    seq_out, seq_in = _tag_seq(out), _tag_seq(chunk)
    if seq_out == seq_in:
        return _restore_tags(out, chunk)        # 属性(href/class…)按位还原为原文
    if sorted(seq_out) == sorted(seq_in):       # 标签仅随语序移动 → 按名分组还原
        return _restore_tags_by_name(out, chunk)
    return chunk                                # 标签增删 → 弃用译文


# ---------------------------------------------------------------- 单文件修复

def fix_comparison(path: Path) -> tuple[bool, str]:
    html = path.read_text(encoding="utf-8")
    new = normalize_labels(html)
    resid = residual_legacy(new)
    note = f"残留:{resid}" if resid else ""
    if new == html:
        return False, note
    _backup(path, html)
    path.write_text(new, encoding="utf-8")
    return True, note


def fix_description(path: Path, scan_only: bool = False) -> tuple[bool, str]:
    html = path.read_text(encoding="utf-8")
    blocks = balanced_div_blocks(html, "node-content") + balanced_div_blocks(html, "verdict-content")
    # 收集需要翻译的 (块内偏移无关) 翻译单元
    jobs: list[str] = []
    plan: list[tuple[int, int, list[str]]] = []  # (start, end, chunks)
    for start, end, inner in blocks:
        chunks = group_chunks(top_level_segments(inner))
        if "".join(chunks) != inner:            # 切块必须无损，否则整块跳过
            continue
        if any(lang_guard.needs_translation(c) for c in chunks):
            plan.append((start, end, chunks))
            jobs.extend(c for c in chunks if lang_guard.needs_translation(c))
    if not plan:
        return False, "无英文块"
    if scan_only:
        return False, f"{len(plan)} 块 / {len(jobs)} 片待翻译"

    uniq = list(dict.fromkeys(jobs))
    with ThreadPoolExecutor(max_workers=8) as ex:
        trans = dict(zip(uniq, ex.map(safe_translate, uniq)))

    pieces: list[str] = []
    pos = 0
    changed = False
    for start, end, chunks in sorted(plan, key=lambda x: x[0]):
        if start < pos:      # 块重叠（理论不应出现）→ 跳过后块保结构
            continue
        pieces.append(html[pos:start])
        new_inner = "".join(
            trans.get(c, c) if lang_guard.needs_translation(c) else c for c in chunks
        )
        if new_inner != html[start:end]:
            changed = True
        pieces.append(new_inner)
        pos = end
    pieces.append(html[pos:])
    if not changed:
        return False, f"{len(uniq)} 片翻译均被安全阀拦下或失败"
    _backup(path, html)
    path.write_text("".join(pieces), encoding="utf-8")
    return True, f"翻译 {len(uniq)} 片"


def _backup(path: Path, original: str) -> None:
    bak = BAK / path.parent.name / path.name
    if not bak.exists():
        bak.parent.mkdir(parents=True, exist_ok=True)
        bak.write_text(original, encoding="utf-8")


# ---------------------------------------------------------------- 主流程

def main() -> None:
    scan_only = "--scan-only" in sys.argv[1:]
    targets = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not scan_only:
        # lang_guard._get_client 惰性初始化非线程安全：并发下其余 worker 会在初始化完成前
        # 拿到 None 而静默跳过翻译。开线程池前先同步初始化一次。
        if lang_guard._get_client() is None:
            print("LLM 客户端初始化失败（无 key / config.toml 有误），中止。", file=sys.stderr)
            sys.exit(1)
    dirs = sorted(d for d in RBW.iterdir() if d.is_dir())
    if targets:
        dirs = [d for d in dirs if d.name in targets]
    print(f"作品目录：{len(dirs)} 个（scan_only={scan_only}）", flush=True)
    n_comp = n_desc = 0
    for d in dirs:
        comp, desc = d / "comparison.html", d / "description.html"
        msgs = []
        if comp.exists():
            if scan_only:
                h = comp.read_text(encoding="utf-8")
                if normalize_labels(h) != h:
                    msgs.append("comp:待归一")
            else:
                ch, note = fix_comparison(comp)
                if ch:
                    n_comp += 1
                    msgs.append("comp:已归一" + (f"({note})" if note else ""))
                elif note:
                    msgs.append(f"comp:{note}")
        if desc.exists():
            ch, note = fix_description(desc, scan_only=scan_only)
            if ch:
                n_desc += 1
                msgs.append(f"desc:{note}")
            elif note and note != "无英文块":
                msgs.append(f"desc:{note}")
        print(f"{d.name}: {'; '.join(msgs) if msgs else 'OK（无需改动）'}", flush=True)
    print(f"完成：comparison 改写 {n_comp} 份，description 改写 {n_desc} 份", flush=True)


if __name__ == "__main__":
    main()
