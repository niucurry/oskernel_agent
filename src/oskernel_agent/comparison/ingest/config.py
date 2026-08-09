"""repos.yaml 的配置模型与读写。"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class RepoEntry(BaseModel):
    """repos.yaml 中的一条仓库记录。"""

    repo_url: str = Field(..., description="GitLab 仓库 HTTP(S) 地址")
    year: int = Field(..., description="参赛/决赛年份")
    team_name: str = Field(..., description="队伍名（用作落盘目录名）")
    award_level: str = Field(default="", description="获奖等级，如 一等奖/二等奖")

    @property
    def repo_id(self) -> str:
        """跨模块统一的仓库标识，与落盘目录 data/repos/{year}/{team_name} 对应。"""
        return f"{self.year}/{self.team_name}"

    @property
    def rel_dir(self) -> Path:
        return Path(str(self.year)) / self.team_name


# 模板内置 3 条示例数据
_TEMPLATE_ENTRIES = [
    {
        "repo_url": "https://gitlab.com/group-2023/team-alpha-os",
        "year": 2023,
        "team_name": "team_alpha",
        "award_level": "一等奖",
    },
    {
        "repo_url": "https://gitlab.com/group-2023/team-beta-os",
        "year": 2023,
        "team_name": "team_beta",
        "award_level": "二等奖",
    },
    {
        "repo_url": "https://gitlab.com/group-2024/team-gamma-os",
        "year": 2024,
        "team_name": "team_gamma",
        "award_level": "三等奖",
    },
]

_TEMPLATE_HEADER = (
    "# 历史决赛作品清单（oskernel_agent.comparison.ingest 数据获取输入）\n"
    "# 每条记录字段：repo_url / year / team_name / award_level\n"
    "# 仓库会被克隆到 data/repos/{year}/{team_name}/\n"
    "# 下面 3 条为示例，请替换为真实仓库后运行：python -m oskernel_agent.comparison.ingest --config config/repos.yaml\n"
)


def load_repos(path: str | Path) -> list[RepoEntry]:
    """读取 repos.yaml，返回 RepoEntry 列表。

    顶层既支持 list，也支持 ``{repos: [...]}`` 形式。
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"配置文件不存在：{path}。可先运行 `python -m oskernel_agent.comparison.ingest --init-template` 生成模板。"
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    if isinstance(data, dict):
        data = data.get("repos", [])
    return [RepoEntry.model_validate(item) for item in data]


def write_template(path: str | Path, *, overwrite: bool = False) -> Path:
    """生成含 3 条示例数据的 repos.yaml 模板。"""
    path = Path(path)
    if path.exists() and not overwrite:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(
        {"repos": _TEMPLATE_ENTRIES},
        allow_unicode=True,
        sort_keys=False,
    )
    path.write_text(_TEMPLATE_HEADER + body, encoding="utf-8")
    return path
