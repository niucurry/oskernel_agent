"""
OS 内核代码分析智能体 — OpenCode 封装入口

架构（OpenCode 作为底层 Agent 运行时）：
  1. 本脚本：解析 CLI 参数、克隆远程仓库、构造用户请求消息
  2. Agent 执行（opencode 子进程）：LLM 调用 + 上下文管理 + 长会话稳定
  3. MCP 服务（mcp_server.py，由 opencode 管理）：
     initialize_analysis → 静态分析 + 返回代码地图
     其他工具 → 符号查询、调用链分析、相似度比对

"""

import os
import subprocess
import sys
from pathlib import Path

import config
from engines.base import AnalysisEngine
from parser.code_parser import (
    find_source_roots,
    classify_files_by_content,
    detect_naming_style,
    find_doc_files,
    detect_anomalies,
)

# OpenCode 可执行文件路径（由 npm install -g opencode-ai 安装）
_OPENCODE = str(Path.home() / ".local" / "bin" / "opencode")


# 引擎选择：路径 A、B、C 依次降级

def select_engine(repo_path: str, profile: dict, level2_index) -> AnalysisEngine:
    primary_lang = profile["primary_lang"]
    ecfg = config.engine

    if primary_lang == "rust" or profile.get("has_cargo"):
        from engines.path_a import RustAnalyzerEngine
        engine = RustAnalyzerEngine(repo_path, level2_index,
                                    timeout=ecfg["rust_analyzer_timeout"])
        if engine.initialize():
            print("[引擎选择] 路径 A：rust-analyzer")
            return engine

    if primary_lang == "c":
        from engines.path_b import ClangdEngine, try_generate_compile_commands
        cc_path = try_generate_compile_commands(repo_path)
        if cc_path:
            engine = ClangdEngine(repo_path, cc_path, level2_index,
                                  timeout=ecfg["clangd_timeout"])
            if engine.initialize():
                print("[引擎选择] 路径 B：clangd")
                return engine

    from engines.path_c import TreeSitterEngine
    lang = primary_lang if primary_lang in ("c", "rust") else "c"
    print(f"[引擎选择] 路径 C：tree-sitter（{lang}）")
    return TreeSitterEngine(repo_path, lang, skip_dirs=ecfg["skip_dirs"])


# 静态分析辅助函数（供本文件和 mcp_server.py 共用）

def _build_structure(repo_path: Path) -> dict:
    source_roots_rel = find_source_roots(repo_path)
    source_roots_abs = [str(repo_path / r) for r in source_roots_rel]
    all_depths = [
        len(f.relative_to(repo_path).parts)
        for ext in ("*.c", "*.rs")
        for f in repo_path.rglob(ext)
    ]
    avg_depth  = sum(all_depths) / len(all_depths) if all_depths else 0.0
    depth_label = "flat" if avg_depth <= 2 else ("shallow" if avg_depth <= 4 else "deep")
    structure = {
        "doc_files":           find_doc_files(repo_path),
        "subsystem_locations": classify_files_by_content(str(repo_path), source_roots_rel),
        "structure_depth":     depth_label,
        "avg_depth":           round(avg_depth, 1),
        "source_roots":        source_roots_abs,
        "source_roots_rel":    source_roots_rel,
        "naming_style":        detect_naming_style(repo_path),
    }
    structure["anomalies"] = detect_anomalies(structure, repo_path)
    return structure


def _resolve_repo_path(url: str | None, repo_path_arg: str | None,
                       repo_id: str | None) -> tuple[Path, str]:
    if url:
        from fetch_single_repo import fetch_repo
        local = fetch_repo(url,
                           output_dir=config.data["repos_dir"],
                           meta_dir=config.data.get("metadata_dir", "data/metadata"))
        p = Path(local).resolve()
        return p, p.name
    if repo_path_arg:
        p = Path(repo_path_arg).resolve()
        return p, p.name
    name = repo_id or config.target.get("repo_id", "")
    if not name:
        import sys
        print(
            "[错误] 未指定仓库。请使用以下任意一种方式：\n"
            "  --repo-id REPO_NAME       分析 data/historical_repos/ 下的已克隆仓库\n"
            "  --repo-path /path/to/repo 分析本地任意路径\n"
            "  --url https://...         克隆远程仓库后分析\n"
            "或在 config.toml 的 [target] 下填写 repo_id = \"REPO_NAME\"",
            file=sys.stderr,
        )
        sys.exit(1)
    p = (Path(config.data["repos_dir"]) / name).resolve()
    return p, name


# OpenCode 调用

def _opencode_env() -> dict:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).parent)
    local_bin = str(Path.home() / ".local" / "bin")
    if local_bin not in env.get("PATH", ""):
        env["PATH"] = local_bin + ":" + env.get("PATH", "")
    return env


def _run_opencode(user_request: str, session_id: str | None = None,
                  continue_last: bool = False, output_file: str = "") -> None:
    """向主分析 agent 发送一条消息并等待完成。

    session_id: 仅在需要继续已有会话时传入（用户手动指定 --session）。
    continue_last: True 时追加 --continue 以续接上一次会话（用于反馈修订循环）。
    """
    cmd = [
        _OPENCODE, "run",
        "--agent", "os-kernel-analyzer",
        "--dangerously-skip-permissions",
    ]
    if session_id:
        cmd += ["--session", session_id]
    elif continue_last:
        cmd += ["--continue"]
    cmd.append(user_request)

    label = session_id or ("续接上一会话" if continue_last else "新会话")
    print(f"\n[OpenCode] 主 agent（{label}）\n")
    proc = subprocess.run(cmd, env=_opencode_env())
    if proc.returncode not in (0, 1):
        print(f"[警告] opencode 退出码 {proc.returncode}，可能存在异常。", file=sys.stderr)

    if output_file and not Path(output_file).exists():
        print("[警告] 报告文件未生成，请检查 write_report 工具是否被调用。",
              file=sys.stderr)


def _run_verifier(report_path: str, repo_path: str) -> str:
    """启动核验 agent，从临时文件读取结构化结果并返回。

    核验 agent 被要求调用 write_report 将结果写入临时文件，
    避免从 OpenCode 的 stdout（含工具调用日志）中解析结论。
    """
    import tempfile
    import time

    report_text = Path(report_path).read_text(encoding="utf-8")
    if len(report_text) > 12000:
        report_text = (
            report_text[:12000]
            + "\n\n...[报告已截断，后续内容请通过 read_file 读取报告文件]..."
        )

    # 为本次核验创建临时输出路径
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".md", prefix="verify_")
    import os
    os.close(tmp_fd)
    Path(tmp_path).unlink()  # 让 write_report 自行创建，避免空文件干扰判断

    request = (
        f"请核验以下评审报告。仓库路径：{repo_path}\n"
        f"核验报告路径：{tmp_path}\n\n"
        f"报告全文：\n\n{report_text}"
    )

    cmd = [
        _OPENCODE, "run",
        "--agent", "os-kernel-verifier",
        "--dangerously-skip-permissions",
        request,
    ]

    proc = subprocess.Popen(
        cmd, env=_opencode_env(),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    stdout, _ = proc.communicate()

    if proc.returncode not in (0, 1):
        print(f"[警告] 核验 agent 退出码 {proc.returncode}。", file=sys.stderr)

    # 优先从临时文件读取结构化结果
    tmp = Path(tmp_path)
    if tmp.exists() and tmp.stat().st_size > 0:
        output = tmp.read_text(encoding="utf-8")
        tmp.unlink(missing_ok=True)
        print(f"[核验] 从临时文件读取核验结果（{len(output)} 字符）")
        return output

    # 临时文件不存在时降级：从 stdout 提取（打印末尾供调试）
    print("[核验] 临时文件未生成，降级为解析 stdout。", file=sys.stderr)
    tail = stdout.strip()[-3000:] if stdout.strip() else ""
    if tail:
        print(f"[核验 stdout 末尾]\n{tail}")
    else:
        print("[警告] 核验 agent 无任何输出。", file=sys.stderr)
    return stdout


def _has_issues(verifier_output: str) -> bool:
    """判断核验结果里是否存在需要修改的问题。

    检查两个章节：
    - "无法访问的引用文件"：有实质内容则说明存在路径错误
    - "需要补充依据的结论"：有实质内容则说明有内容不符或缺引用的问题

    空输出视为核验失败（进程异常），返回 False 跳过修订，
    但会在调用侧打印警告。
    """
    import re
    if not verifier_output.strip():
        return False

    _empty = {"无", "无问题", "（无）", "暂无", "无缺乏依据的结论", "无需要补充依据的结论",
              "无无法访问的引用文件", "全部文件路径均有效"}

    for pattern in (
        r"##\s*无法访问的引用文件.{0,20}\n(.*?)(?=\n##|\Z)",
        r"##\s*(?:需要补充依据|缺乏依据).{0,30}\n(.*?)(?=\n##|\Z)",
    ):
        m = re.search(pattern, verifier_output, re.DOTALL)
        if m:
            body = m.group(1).strip()
            if body and body not in _empty:
                return True

    # 如果输出里根本没有期望的章节标头，说明 LLM 没有按格式输出
    if "## 无法访问的引用文件" not in verifier_output and \
       "## 需要补充依据" not in verifier_output and \
       "## 缺乏依据" not in verifier_output:
        print("[核验] 输出中未找到预期章节标头，无法解析核验结果。", file=sys.stderr)

    return False


def _send_feedback(verifier_output: str, output_file: str) -> None:
    """续接上一个会话，把核验结果发给主 agent 要求修订。

    主 agent 有完整的历史上下文（知道自己分析了什么、报告写了什么），
    只需重新 initialize_analysis 即可继续用工具查找缺失依据。
    """
    feedback = (
        f"独立核验 agent 检查了你刚才生成的报告，发现以下问题：\n\n"
        f"{verifier_output}\n\n"
        f"请根据上述核验结果依次处理两类问题：\n"
        f"  1. 【无法访问的引用文件】——这些文件路径在仓库中不存在，"
        f"用 search_code 按函数名定位正确路径后更新报告中的 路径:行号；"
        f"若确实不存在则改写为\"未找到相关实现\"。\n"
        f"  2. 【需要补充依据的结论】——引用位置不支撑或完全没有引用的结论，"
        f"用 read_file / search_code / find_symbol_definition 确认后补充正确的 路径:行号。\n\n"
        f"步骤：先调用 initialize_analysis 重新初始化仓库上下文，"
        f"逐条查找补充，完成后调用 write_report 将修订版保存到原路径（{output_file}）。"
    )
    _run_opencode(feedback, continue_last=True, output_file=output_file)


# 主入口

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(add_help=False)

    src = parser.add_mutually_exclusive_group()
    src.add_argument("--repo-id")
    src.add_argument("--repo-path")
    src.add_argument("--url")

    parser.add_argument("--output", "-o")
    parser.add_argument("--model")
    parser.add_argument("--session", "-s")

    args = parser.parse_args()

    if args.model:
        config.api["model"] = args.model

    # 解析仓库路径（静态分析由 initialize_analysis 工具在 MCP server 端执行）
    repo_path_a, repo_name_a = _resolve_repo_path(args.url, args.repo_path, args.repo_id)

    # 报告输出路径：未指定 --output 时按 data/reports/<repo>_<时间戳>.md 自动生成
    if args.output:
        output_file = str(Path(args.output).resolve())
    else:
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        reports_dir = Path(config.data.get("reports_dir", "./data/reports")).resolve()
        reports_dir.mkdir(parents=True, exist_ok=True)
        output_file = str(reports_dir / f"{repo_name_a}_{ts}.md")

    output_hint = (
        f"\n\n【报告输出要求（必须执行）】完成所有分析后，"
        f"调用 write_report 工具将完整 Markdown 报告写入文件："
        f"\n  write_report(content=\"<完整报告>\", output_path=\"{output_file}\")"
        f"\n不要把报告内容直接输出到对话中；只调用 write_report 工具即可。"
    )

    user_request = (
        f"请分析项目 {repo_name_a}（路径：{repo_path_a}）。"
        f"{output_hint}"
    )

    session_id = args.session or None
    label = f"会话：{session_id}" if session_id else "新会话"
    print(f"[agent] 报告将写入：{output_file}  {label}")
    _run_opencode(user_request, session_id=session_id, output_file=output_file)

    # 核验循环：核验 agent 静默检查，有问题则续接上一会话让主 agent 修订
    MAX_REVISIONS = 2
    primary_repo = str(repo_path_a)
    for revision in range(MAX_REVISIONS + 1):
        if not Path(output_file).exists():
            print("[agent] 报告文件不存在，跳过核验。", file=sys.stderr)
            break

        print(f"\n[agent] 第 {revision + 1} 次核验中...", end=" ", flush=True)
        verifier_output = _run_verifier(output_file, primary_repo)

        if not _has_issues(verifier_output):
            print("核验通过，无缺乏依据的结论。")
            break

        print("发现缺乏依据的结论。")
        if revision == MAX_REVISIONS:
            print(f"[agent] 已达最大修订轮数（{MAX_REVISIONS}），流程结束。")
            break

        print(f"[agent] 续接会话追加核验反馈，主 agent 继续修订...\n")
        _send_feedback(verifier_output, output_file)

    if Path(output_file).exists():
        print(f"\n[完成] 最终报告：{output_file}")
        html_p = Path(output_file).with_suffix(".html")
        if html_p.exists():
            print(f"[完成] HTML 报告：{html_p}")
