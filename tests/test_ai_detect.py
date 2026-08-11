"""oskernel_agent.comparison.ai_detect 测试：抽取桥接 + 检测编排（mock LogRankProvider，不加载真实模型）+ 报告章六。

设计与项目约定一致：模块在无真实模型时用注入的 mock 独立可测。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from oskernel_agent.comparison.ai_detect.extract import extract_blocks
from oskernel_agent.comparison.ai_detect.runner import run_ai_detect
from oskernel_agent.comparison.ai_detect.settings import AIDetectSettings, load_ai_detect_settings
from oskernel_agent.comparison.ai_detect.vendor.ai_code_detector.models import Language
from oskernel_agent.comparison.ai_detect.vendor.ai_code_detector.perplexity import (
    _auto_device_memory_limits,
)
from oskernel_agent.comparison.pipeline.__main__ import build_parser


# ---------- mock provider ----------

class FakeScorer:
    """按代码内容给确定性的 log-rank：含 'ai_like' → 低（判 LLM），否则高（判 Human）。

    扰动只改空白，标记仍在，stage-2 不会触发（fast-filter 已决断）。
    """

    def compute_log_rank(self, code: str) -> float:
        return 0.5 if "ai_like" in code else 3.5

    def compute_log_rank_batch(self, codes):
        return [self.compute_log_rank(c) for c in codes]


def _long_rust(name: str, marker_var: str, n: int = 26) -> str:
    body = "\n".join(f"    let v{i} = {marker_var} + {i};" for i in range(n))
    return f"pub fn {name}() -> i64 {{\n    let {marker_var} = 1;\n{body}\n    v0\n}}\n"


@pytest.fixture()
def mini_repo(tmp_path: Path) -> Path:
    src = tmp_path / "os" / "src"
    src.mkdir(parents=True)
    # 一个“AI 疑似”长函数 + 一个“人类”长函数
    (src / "lib.rs").write_text(
        _long_rust("generated_fn", "ai_like") + "\n" + _long_rust("hand_written", "humanvar"),
        encoding="utf-8",
    )
    # C 文件
    (src / "util.c").write_text(
        "int compute(int x) {\n" + "\n".join(f"    x += {i};" for i in range(24)) + "\n    return x;\n}\n",
        encoding="utf-8",
    )
    # asm 应被跳过（上游不支持）
    (src / "boot.S").write_text("_start:\n    li a0, 1\n    ret\n", encoding="utf-8")
    return tmp_path


# ---------- 抽取桥接 ----------

def test_extract_bridges_rust_c_skips_asm(mini_repo: Path):
    blocks = extract_blocks(mini_repo)
    langs = {b.language for b in blocks}
    assert Language.RUST in langs and Language.C in langs
    assert all(b.language != "asm" for b in blocks)  # asm 不产出 block
    names = {b.name for b in blocks}
    assert {"generated_fn", "hand_written", "compute"} <= names
    # file_path 为绝对路径，loc 为有效行数（>0）
    assert all(b.file_path.is_absolute() and b.loc > 0 for b in blocks)


def test_extract_max_functions_limit(mini_repo: Path):
    assert len(extract_blocks(mini_repo, max_functions=1)) == 1


# ---------- 检测编排 ----------

def test_run_ai_detect_ok_with_mock(mini_repo: Path):
    # 阈值显式给定（FakeScorer 契约：0.5→LLM、3.5→Human），不依赖生产校准默认值
    st = AIDetectSettings(min_loc=20, suspicious_min_confidence=0.4, git_blame=False,
                          log_rank_llm_threshold=1.5, log_rank_human_threshold=3.0)
    res = run_ai_detect(mini_repo, settings=st, scorer=FakeScorer(),
                        show_progress=False, write=False)
    assert res["status"] == "ok"
    overall = res["aggregated"]["overall"]
    assert overall["total_functions"] >= 3
    assert overall["llm_count"] >= 1          # generated_fn 被判 LLM
    assert overall["human_count"] >= 1        # hand_written / compute 被判 Human
    susp_names = {s["function_name"] for s in res["aggregated"]["suspicious_functions"]}
    assert "generated_fn" in susp_names


def test_run_ai_detect_writes_json(mini_repo: Path, tmp_path: Path):
    out = tmp_path / "out"
    st = AIDetectSettings(min_loc=20, git_blame=False)
    res = run_ai_detect(mini_repo, output_dir=out, repo_name="demo",
                        settings=st, scorer=FakeScorer(), show_progress=False)
    p = out / "demo_ai_detect.json"
    assert p.exists() and res["output_path"] == str(p)


def test_run_ai_detect_excludes_borrowed(tmp_path: Path):
    # 排除借鉴模式：文件级 + 函数级借鉴代码跳过，只检测未匹配上的原创函数
    src = tmp_path / "os" / "src"
    src.mkdir(parents=True)
    (src / "lib.rs").write_text(
        _long_rust("borrowed_fn", "v", n=26) + "\n"     # 函数级借鉴 → 跳过
        + _long_rust("original_fn", "w", n=26),         # 未匹配 → 保留
        encoding="utf-8",
    )
    (src / "copied.rs").write_text(
        _long_rust("whole_file_fn", "u", n=26),         # 文件级借鉴 → 整文件跳过
        encoding="utf-8",
    )
    st = AIDetectSettings(min_loc=20, git_blame=False)
    res = run_ai_detect(
        tmp_path, settings=st, scorer=FakeScorer(),
        show_progress=False, write=False,
        exclude_files={"os/src/copied.rs"},
        exclude_funcs={("os/src/lib.rs", "borrowed_fn")},
    )
    assert res["status"] == "ok"
    # 只有 original_fn 进入检测（borrowed_fn / whole_file_fn 均被排除）
    assert res["aggregated"]["overall"]["total_functions"] == 1


def test_run_ai_detect_excludes_registered_third_party_libraries(tmp_path: Path):
    own = tmp_path / "kernel" / "src"
    third_party = tmp_path / "crates" / "smoltcp" / "src"
    own.mkdir(parents=True)
    third_party.mkdir(parents=True)
    (own / "lib.rs").write_text(
        _long_rust("own_impl", "own_marker"), encoding="utf-8")
    (third_party / "lib.rs").write_text(
        _long_rust("vendored_impl", "ai_like"), encoding="utf-8")

    res = run_ai_detect(
        tmp_path, settings=AIDetectSettings(min_loc=20, git_blame=False),
        scorer=FakeScorer(), show_progress=False, write=False,
    )

    assert res["status"] == "ok"
    assert res["aggregated"]["overall"]["total_functions"] == 1
    assert res["scope"]["third_party_excluded"] == 1
    assert res["scope"]["eligible_functions"] == 1


def test_run_ai_detect_excludes_verified_library_adapter(tmp_path: Path):
    own = tmp_path / "os" / "src"
    adapter = own / "fs" / "ext4_lw"
    package = tmp_path / "crates" / "renamed-ext4-package"
    own.mkdir(parents=True)
    adapter.mkdir(parents=True)
    package.mkdir(parents=True)
    (package / "Cargo.toml").write_text(
        '[package]\nname = "lwext4_rust"\nversion = "0.1.0"\n', encoding="utf-8")
    (own / "lib.rs").write_text(
        _long_rust("own_impl", "own_marker"), encoding="utf-8")
    (adapter / "inode.rs").write_text(
        "use lwext4_rust::Ext4File;\n" + _long_rust("adapter_impl", "ai_like"),
        encoding="utf-8",
    )

    res = run_ai_detect(
        tmp_path, settings=AIDetectSettings(min_loc=20, git_blame=False),
        scorer=FakeScorer(), show_progress=False, write=False,
    )

    assert res["status"] == "ok"
    assert res["aggregated"]["overall"]["total_functions"] == 1
    assert res["scope"]["third_party_excluded"] == 1


def test_run_ai_detect_skips_when_no_functions(tmp_path: Path):
    (tmp_path / "readme.md").write_text("no code here", encoding="utf-8")
    res = run_ai_detect(tmp_path, settings=AIDetectSettings(), scorer=FakeScorer(),
                        show_progress=False, write=False)
    assert res["status"] == "skipped" and "rust/c" in res["reason"]
    assert res["scope"]["eligible_functions"] == 0
    assert res["scope"]["analyzed_functions"] == 0
    assert len(res["source_fingerprint"]) == 64


def test_write_false_creates_no_output_directory(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "readme.md").write_text("no code here", encoding="utf-8")
    res = run_ai_detect(tmp_path, settings=AIDetectSettings(), scorer=FakeScorer(),
                        show_progress=False, write=False)
    assert res["status"] == "skipped"
    assert not (tmp_path / "data").exists()


# ---------- 配置 ----------

def test_settings_env_override(monkeypatch):
    monkeypatch.setenv("AI_DETECT_LR_LLM", "1.2")
    monkeypatch.setenv("AI_DETECT_MODEL", "bigcode/starcoder2-3b")
    load_ai_detect_settings.cache_clear()
    st = load_ai_detect_settings()
    assert st.log_rank_llm_threshold == 1.2 and st.model_id == "bigcode/starcoder2-3b"
    load_ai_detect_settings.cache_clear()


def test_cuda_memory_limits_reserve_inference_headroom(monkeypatch):
    monkeypatch.delenv("AI_DETECT_CUDA_MAX_MEMORY", raising=False)
    monkeypatch.setenv("AI_DETECT_CUDA_RESERVE_GIB", "2")
    monkeypatch.setenv("AI_DETECT_CPU_MAX_MEMORY", "12GiB")
    monkeypatch.setattr("torch.cuda.mem_get_info", lambda _device: (8 * 1024 ** 3, 8 * 1024 ** 3))
    monkeypatch.setattr("torch.cuda.current_device", lambda: 0)

    assert _auto_device_memory_limits(__import__("torch").device("cuda")) == {
        0: "6GiB",
        "cpu": "12GiB",
    }


def test_pipeline_runs_ai_model_by_default_and_allows_explicit_skip():
    assert build_parser().parse_args(["--repo", "demo"]).ai_detect is True
    assert build_parser().parse_args(
        ["--repo", "demo", "--skip-ai-detect"]).ai_detect is False


# 说明：旧 Markdown 报告（oskernel_agent.comparison.report.generate）的「章六」拼装已随旧流程一并移除；
# AI 检测章节现由 semantic_compare 直接渲染进对比报告 HTML，测试见 test_report.py。
