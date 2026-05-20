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
from prompts import SessionType, SESSION_AGENT_NAMES, SESSION_FRAG_SUFFIX, SESSION_SEQUENCE

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


def _run_opencode(user_request: str, agent_name: str = "os-kernel-analyzer",
                  session_id: str | None = None,
                  continue_last: bool = False, output_file: str = "") -> None:
    """向指定 agent 发送一条消息并等待完成。

    agent_name: OpenCode 中注册的 agent 名称。
    session_id: 仅在需要继续已有会话时传入（用户手动指定 --session）。
    continue_last: True 时追加 --continue 以续接上一次会话（用于反馈修订循环）。
    """
    cmd = [
        _OPENCODE, "run",
        "--agent", agent_name,
        "--dangerously-skip-permissions",
    ]
    if session_id:
        cmd += ["--session", session_id]
    elif continue_last:
        cmd += ["--continue"]
    cmd.append(user_request)

    label = session_id or ("续接上一会话" if continue_last else "新会话")
    print(f"\n[OpenCode] {agent_name}（{label}）\n")
    proc = subprocess.run(cmd, env=_opencode_env())
    if proc.returncode not in (0, 1):
        print(f"[警告] opencode 退出码 {proc.returncode}，可能存在异常。", file=sys.stderr)

    if output_file and not Path(output_file).exists():
        print("[警告] 报告文件未生成，请检查 write_report 工具是否被调用。",
              file=sys.stderr)


def _run_multi_session(repo_path: Path, repo_name: str, output_file: str) -> None:
    """多会话模式主流程：依次运行各分析会话，最后合并。"""
    from datetime import datetime
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    frag_dir = Path(output_file).parent / "fragments"
    frag_dir.mkdir(parents=True, exist_ok=True)

    fragment_paths: list[str] = []

    for session_type in SESSION_SEQUENCE:
        agent_name = SESSION_AGENT_NAMES[session_type]
        suffix = SESSION_FRAG_SUFFIX[session_type]
        frag_path = str(frag_dir / f"{repo_name}_{suffix}_{ts}.html")

        output_hint = (
            f"\n\n【输出要求】完成本会话负责的章节分析后，调用 write_report 写入分片文件"
            f"（工具自动渲染为 HTML）：\n"
            f"  write_report(content=\"<分片报告>\", output_path=\"{frag_path}\")\n"
            f"只输出本会话负责的章节，不要分析其他章节。"
        )
        request = f"请分析项目 {repo_name}（路径：{repo_path}）。{output_hint}"

        print(f"\n[多会话] 启动 {agent_name} ...")
        _run_opencode(request, agent_name=agent_name, output_file=frag_path)

        if Path(frag_path).exists():
            fragment_paths.append(frag_path)
            print(f"[多会话] {agent_name} 完成，分片：{frag_path}")
        else:
            print(f"[警告] {agent_name} 未生成分片，跳过。", file=sys.stderr)

    if not fragment_paths:
        print("[错误] 所有分会话均未生成分片，放弃合并。", file=sys.stderr)
        return

    frag_list = "\n".join(f"  - {p}" for p in fragment_paths)
    merge_request = (
        f"请合并以下分片报告为完整评审报告。\n"
        f"仓库路径：{repo_path}\n\n"
        f"分片文件列表：\n{frag_list}\n\n"
        f"【输出要求】合并完成后调用 write_report 写入最终报告"
        f"（工具自动渲染为 HTML）：\n"
        f"  write_report(content=\"<完整报告>\", output_path=\"{output_file}\")"
    )

    print(f"\n[多会话] 启动 os-kernel-merge（合并 {len(fragment_paths)} 个分片）...")
    _run_opencode(merge_request, agent_name="os-kernel-merge", output_file=output_file)



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

    args = parser.parse_args()

    if args.model:
        config.api["model"] = args.model

    repo_path_a, repo_name_a = _resolve_repo_path(args.url, args.repo_path, args.repo_id)

    if args.output:
        output_file = str(Path(args.output).resolve())
    else:
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        reports_dir = Path(config.data.get("reports_dir", "./data/reports")).resolve()
        reports_dir.mkdir(parents=True, exist_ok=True)
        output_file = str(reports_dir / f"{repo_name_a}_{ts}.html")

    print(f"[agent] 报告将写入：{output_file}")
    _run_multi_session(repo_path_a, repo_name_a, output_file)

    if Path(output_file).exists():
        print(f"\n[完成] 最终报告：{output_file}")
