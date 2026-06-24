"""AI 生成代码检测模块（独立于查重四层漏斗，单独成模块）。

把上游 ai-code-detector（DetectCodeGPT，困惑度/log-rank 免训练检测）接入本系统：

- 复用本仓库 `src.normalize` 的 tree-sitter 解析（rust/c）抽取函数，桥接为上游 FunctionBlock，
  避免引入与本仓库冲突的 `tree-sitter-languages`（见 vendor/README.md）。
- 检测结果落盘 `{repo}_ai_detect.json`，与其他模块一样文件级解耦。
- 报告环节由 `src.report` 单独「唤起一个会话」生成「AI 生成代码检测」章节并拼入最终报告。

CLI：`python -m src.ai_detect --repo <路径>`
"""

from .runner import run_ai_detect

__all__ = ["run_ai_detect"]
