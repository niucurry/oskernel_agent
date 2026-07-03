# -*- coding: utf-8 -*-
"""对收敛后仍含英文整句的顽固片段做强制翻译：
- 绕过 lang_guard.needs_translation（这些片段因代码密度高被它判为不译）
- 译文多出的标签（模型自作主张包的 <code>/多余 </p>）按名删余后再按位还原原文标签
安全阀不变：标签多重集（删余后）必须与原文一致，正文不得异常缩水。
"""
import re
import sys
from collections import Counter, defaultdict, deque
from pathlib import Path

ROOT = Path(r"d:\agent\project3136859-379280")
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # fix_workid_reports 同在 scripts/
import fix_workid_reports as F  # noqa: E402
from oskernel_agent.pipeline import lang_guard  # noqa: E402

SENT = re.compile(r"\b[A-Za-z][a-z]+(?:\s+(?:[A-Za-z][a-z']*|a|an|the|of|to|in|is|are|and|or|for|with)){7,}")


def has_english_sentence(c: str) -> bool:
    t = re.sub(r"<code\b[^>]*>.*?</code>", " ", c, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"https?://\S+|[\w./\\-]+\.[A-Za-z0-9]+(?::\d+(?:-\d+)?)?", " ", t)
    t = re.sub(r"[\w-]+/[\w./-]+", " ", t)
    return bool(SENT.search(t))


def drop_surplus_tags(out: str, src: str) -> str:
    """把译文中「原文没有的多余标签」删掉（保留其内文本），使多重集对齐。"""
    src_cnt = Counter(F._tag_seq(src))
    out_tokens = F._TAGTOKEN.findall(out)
    parts = F._TAGTOKEN.split(out)
    if len(parts) != len(out_tokens) + 1:
        return out
    seen: Counter = Counter()
    rebuilt = parts[0]
    for tok, text in zip(out_tokens, parts[1:]):
        name = F._tag_name(tok)
        seen[name] += 1
        if seen[name] > src_cnt.get(name, 0):
            rebuilt += text          # 丢标签、留内文
        else:
            rebuilt += tok + text
    return rebuilt


def force_translate(chunk: str) -> str | None:
    out = F._call_llm(chunk)
    if not out or out == chunk:
        return None
    out2 = drop_surplus_tags(out, chunk)
    si, so = F._tag_seq(chunk), F._tag_seq(out2)
    if sorted(si) != sorted(so):
        return None                  # 原文有的标签译文丢了 → 弃用
    if F._plain_len(out2) < F._plain_len(chunk) * 0.15:
        return None
    if si == so:
        return F._restore_tags(out2, chunk)
    return F._restore_tags_by_name(out2, chunk)


def main() -> None:
    files = sys.argv[1:]
    n_files = n_ch = 0
    for name in files:
        p = ROOT / "reports_by_work_id" / name / "description.html"
        html = p.read_text(encoding="utf-8")
        blocks = (F.balanced_div_blocks(html, "node-content")
                  + F.balanced_div_blocks(html, "verdict-content"))
        repl: list[tuple[int, int, str]] = []
        for start, end, inner in blocks:
            chunks = F.group_chunks(F.top_level_segments(inner))
            if "".join(chunks) != inner:
                continue
            new_parts = []
            block_changed = False
            for c in chunks:
                if has_english_sentence(c):
                    nv = force_translate(c)
                    if nv and nv != c:
                        new_parts.append(nv)
                        block_changed = True
                        continue
                new_parts.append(c)
            if block_changed:
                repl.append((start, end, "".join(new_parts)))
        if not repl:
            print(f"{name}: 无可改", flush=True)
            continue
        pieces, pos = [], 0
        cnt = 0
        for start, end, new_inner in sorted(repl):
            if start < pos:
                continue
            pieces.append(html[pos:start])
            pieces.append(new_inner)
            pos = end
            cnt += 1
        pieces.append(html[pos:])
        p.write_text("".join(pieces), encoding="utf-8")
        n_files += 1
        n_ch += cnt
        print(f"{name}: 强制翻译 {cnt} 块", flush=True)
    print(f"完成：{n_files} 文件 / {n_ch} 块", flush=True)


if __name__ == "__main__":
    main()
