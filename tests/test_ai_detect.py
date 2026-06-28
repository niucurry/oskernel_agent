"""src.ai_detect 测试：抽取桥接 + 检测编排（mock LogRankProvider，不加载真实模型）+ 报告章六。

设计与项目约定一致：模块在无真实模型时用注入的 mock 独立可测。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.ai_detect.extract import extract_blocks
from src.ai_detect.runner import run_ai_detect
from src.ai_detect.settings import AIDetectSettings, load_ai_detect_settings
from src.ai_detect.vendor.ai_code_detector.models import Language
from src.report.generate import generate_report


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
    st = AIDetectSettings(min_loc=20, suspicious_min_confidence=0.4, git_blame=False)
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


def test_run_ai_detect_skips_when_no_functions(tmp_path: Path):
    (tmp_path / "readme.md").write_text("no code here", encoding="utf-8")
    res = run_ai_detect(tmp_path, settings=AIDetectSettings(), scorer=FakeScorer(),
                        show_progress=False, write=False)
    assert res["status"] == "skipped" and "rust/c" in res["reason"]


# ---------- 配置 ----------

def test_settings_env_override(monkeypatch):
    monkeypatch.setenv("AI_DETECT_LR_LLM", "1.2")
    monkeypatch.setenv("AI_DETECT_MODEL", "bigcode/starcoder2-3b")
    load_ai_detect_settings.cache_clear()
    st = load_ai_detect_settings()
    assert st.log_rank_llm_threshold == 1.2 and st.model_id == "bigcode/starcoder2-3b"
    load_ai_detect_settings.cache_clear()


# ---------- 报告章六（唤起会话生成 + 拼装）----------

class _FakeReportLLM:
    async def complete(self, messages, temperature):
        sysmsg = messages[0]["content"]
        if "AI 生成代码检测" in sysmsg:
            # 一条真实引用（应保留）+ 一条编造引用（应被 scrub 删）
            ref = messages[1]["content"]
            real = ref.split("ref: ")[1].split(",")[0] if "ref: " in ref else "x.rs:1-2"
            return f"整体 AI 疑似偏高，{real} 最可疑。另有 fake.rs:9999 的雷同。"
        return ""


def _ai_report(mini_repo: Path) -> dict:
    st = AIDetectSettings(min_loc=20, suspicious_min_confidence=0.4, git_blame=False)
    return run_ai_detect(mini_repo, settings=st, scorer=FakeScorer(),
                         show_progress=False, write=False)


def test_report_section6_template_fallback(mini_repo: Path):
    ai = _ai_report(mini_repo)
    md, _ = generate_report({"suspects": []}, {"results": []}, client=None, ai_report=ai)
    assert "六、AI 生成代码检测" in md
    assert "DetectCodeGPT" in md                 # 方法学注脚
    assert "高置信 AI 疑似函数" in md            # 代码生成表格
    assert "generated_fn" in md                  # 可疑函数进表


def test_report_section6_llm_session_and_scrub(mini_repo: Path):
    ai = _ai_report(mini_repo)
    md, deleted = generate_report({"suspects": []}, {"results": []},
                                  client=_FakeReportLLM(), ai_report=ai)
    assert "六、AI 生成代码检测" in md
    assert "fake.rs:9999" not in md              # 编造引用被后置校验删除
    assert deleted >= 1


def test_report_section6_missing_and_skipped():
    # 未提供 ai_report
    md, _ = generate_report({"suspects": []}, {"results": []}, client=None, ai_report=None)
    assert "未运行 AI 生成代码检测" in md
    # skipped 状态透传原因
    md2, _ = generate_report({"suspects": []}, {"results": []}, client=None,
                             ai_report={"status": "skipped", "reason": "参考模型不可用（OSError）"})
    assert "未完成" in md2 and "参考模型不可用" in md2
