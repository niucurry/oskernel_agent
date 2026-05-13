"""
OS 内核代码分析智能体 — OpenCode 封装入口

架构（OpenCode 作为底层 Agent 运行时）：
  1. 本脚本：解析 CLI 参数、克隆远程仓库、构造用户请求消息
  2. Agent 执行（opencode 子进程）：LLM 调用 + 上下文管理 + 长会话稳定
  3. MCP 服务（mcp_server.py，由 opencode 管理）：
     initialize_analysis → 静态分析 + 返回代码地图
     其他工具 → 符号查询、调用链分析、相似度比对

前置条件：
  运行一次 setup_opencode.py 以注册 agent 和 MCP server 到全局 OpenCode 配置。
  也可直接使用 OpenCode：
    opencode run --agent os-kernel-analyzer "分析 /path/to/repo"
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

def _run_opencode(user_request: str, session_id: str = "", output_file: str = "") -> None:
    """
    启动 opencode run 子进程，将 LLM 推理和工具调用委托给 OpenCode 运行时。

    OpenCode 负责：
      - LLM API 调用（使用 OPENAI_BASE_URL 重定向到 DeepSeek）
      - 上下文窗口管理和长会话压缩
      - 工具调用解析和 MCP server 通信
      - 会话持久化（SQLite at ~/.local/share/opencode/opencode.db）

    --dangerously-skip-permissions 说明：
      自动批准未被明确拒绝的权限。我们已在 agent config 中明确拒绝
      bash/edit/write，因此该标志只允许 MCP 工具调用通过，不影响安全性。
    """
    cmd = [
        _OPENCODE,
        "run",
        "--agent", "os-kernel-analyzer",
        "--dangerously-skip-permissions",
    ]
    if session_id:
        cmd += ["--session", session_id]
    cmd.append(user_request)

    env = os.environ.copy()
    # PYTHONPATH 让 MCP server 子进程能 import 项目模块
    env["PYTHONPATH"] = str(Path(__file__).parent)
    # 确保 opencode 和 venv Python 均在 PATH 中
    local_bin = str(Path.home() / ".local" / "bin")
    if local_bin not in env.get("PATH", ""):
        env["PATH"] = local_bin + ":" + env.get("PATH", "")

    print(f"\n[OpenCode] Agent 启动（模型：{config.api['model']}，"
          f"会话：{session_id or '新建'}）\n")

    # OpenCode 直接流式输出到用户终端
    proc = subprocess.run(cmd, env=env)

    if proc.returncode not in (0, 1):
        print(f"[警告] opencode 退出码 {proc.returncode}，可能存在异常。", file=sys.stderr)

    if output_file:
        p = Path(output_file)
        if p.exists():
            print(f"\n[完成] 报告已保存至：{output_file}")
        else:
            print(f"[警告] 报告文件未生成，请检查 write_report 工具是否被调用。",
                  file=sys.stderr)


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
    parser.add_argument("--compare", action="store_true")

    bgroup = parser.add_mutually_exclusive_group()
    bgroup.add_argument("--repo-id-b")
    bgroup.add_argument("--repo-path-b")
    bgroup.add_argument("--url-b")

    args = parser.parse_args()

    if args.compare and not any([args.repo_id_b, args.repo_path_b, args.url_b]):
        parser.error("--compare 需要同时指定第二个仓库（--repo-id-b / --repo-path-b / --url-b）")

    if args.model:
        config.api["model"] = args.model

    # 解析仓库路径（静态分析由 initialize_analysis 工具在 MCP server 端执行）
    repo_path_a, repo_name_a = _resolve_repo_path(args.url, args.repo_path, args.repo_id)

    if args.compare:
        repo_path_b, repo_name_b = _resolve_repo_path(
            args.url_b, args.repo_path_b, args.repo_id_b
        )

    # 报告输出路径：未指定 --output 时按 data/reports/<repo>_<时间戳>.md 自动生成
    if args.output:
        output_file = str(Path(args.output).resolve())
    else:
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        reports_dir = Path(config.data.get("reports_dir", "./data/reports")).resolve()
        reports_dir.mkdir(parents=True, exist_ok=True)
        if args.compare:
            fname = f"compare_{repo_name_a}_vs_{repo_name_b}_{ts}.md"
        else:
            fname = f"{repo_name_a}_{ts}.md"
        output_file = str(reports_dir / fname)

    output_hint = (
        f"\n\n【报告输出要求（必须执行）】完成所有分析后，"
        f"调用 write_report 工具将完整 Markdown 报告写入文件："
        f"\n  write_report(content=\"<完整报告>\", output_path=\"{output_file}\")"
        f"\n不要把报告内容直接输出到对话中；只调用 write_report 工具即可。"
    )

    if args.compare:
        user_request = (
            f"请比较项目 {repo_name_a}（路径：{repo_path_a}）"
            f"和项目 {repo_name_b}（路径：{repo_path_b}）。"
            f"{output_hint}"
        )
    else:
        user_request = (
            f"请分析项目 {repo_name_a}（路径：{repo_path_a}）。"
            f"{output_hint}"
        )

    print(f"[agent] 报告将写入：{output_file}")
    _run_opencode(user_request, session_id=args.session or "", output_file=output_file)
