"""
按需加载的技能（skill）注册表。

把**条件性**提示词内容（只在特定场景才需要的指引，如「与参考 OS 相似度对比」）
从静态系统提示词中抽出，做成独立技能文件。系统提示里只保留精简的技能目录
（名字 + 一句话 + 触发条件），模型在命中条件时调用 MCP 工具 load_skill(name)
按需取回完整指引。核心内容（角色 / 硬约束 / 会话工作流 / 输出格式）仍内联。

技能文件位于 prompts/skills/*.md，沿用模板的 HTML 注释分隔约定：
    name: <kebab-case 名字>
    description: <一句话，用于目录展示>
    applies_to: subsys, verdict
    <!-- body -->
    （完整指引正文……）
"""

from dataclasses import dataclass, field
from pathlib import Path

from .builder import SessionType

_SKILLS_DIR = Path(__file__).parent / "skills"
_BODY_MARK = "<!-- body -->"


@dataclass
class Skill:
    name: str
    description: str
    applies_to: list[str] = field(default_factory=list)
    body: str = ""


def _parse_skill(text: str) -> Skill | None:
    parts = text.split(_BODY_MARK, 1)
    header = parts[0]
    body = parts[1].strip() if len(parts) > 1 else ""

    meta: dict[str, str] = {}
    for line in header.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, val = line.partition(":")
        meta[key.strip()] = val.strip()

    name = meta.get("name", "")
    if not name:
        return None
    applies_to = [s.strip() for s in meta.get("applies_to", "").split(",") if s.strip()]
    return Skill(
        name=name,
        description=meta.get("description", ""),
        applies_to=applies_to,
        body=body,
    )


def _load() -> dict[str, Skill]:
    result: dict[str, Skill] = {}
    if not _SKILLS_DIR.is_dir():
        return result
    for path in sorted(_SKILLS_DIR.glob("*.md")):
        skill = _parse_skill(path.read_text(encoding="utf-8"))
        if skill is not None:
            result[skill.name] = skill
    return result


_SKILLS: dict[str, Skill] = _load()


def load_all_skills() -> dict[str, Skill]:
    """返回全部已注册技能（name → Skill）。"""
    return _SKILLS


def build_catalog(session_type: SessionType) -> str:
    """构建某会话适用技能的精简目录段；无适用技能则返回空串。"""
    st = session_type.value
    applicable = [s for s in _SKILLS.values() if st in s.applies_to]
    if not applicable:
        return ""

    lines = [
        "━" * 30,
        "【可按需加载的技能】",
        "",
        "以下能力的详细指引未默认载入，仅在命中其触发条件时，"
        "先调用 `load_skill(name)` 取回完整指引再执行：",
        "",
    ]
    for s in applicable:
        lines.append(f"- `{s.name}` — {s.description}")
    return "\n".join(lines)


def get_skill_body(name: str) -> str:
    """返回技能正文；未知名字返回提示与可用技能列表。"""
    skill = _SKILLS.get(name)
    if skill is None:
        available = ", ".join(_SKILLS.keys()) or "（无）"
        return f"[load_skill] 未找到技能 {name!r}。可用技能：{available}"
    return skill.body
