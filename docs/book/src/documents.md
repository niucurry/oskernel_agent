# 相关文档

## 仓库内文档

| 文档 | 位置 | 内容 |
|---|---|---|
| 项目说明 | [`README.md`](https://github.com/niucurry/oskernel_agent/blob/main/README.md) | 安装、快速使用、目录结构、质量门禁 |
| 项目地图 | [`docs/project-map.md`](https://github.com/niucurry/oskernel_agent/blob/main/docs/project-map.md) | 逐模块说明：每个文件做什么、关键代码在哪、模块之间怎么流动 |
| 设计方案与技术文档 | [`docs/design-specification.pdf`](https://github.com/niucurry/oskernel_agent/blob/main/docs/design-specification.pdf) | 系统的完整设计说明 |
| 决赛报告设计说明 | [`docs/finals-report-plan.md`](https://github.com/niucurry/oskernel_agent/blob/main/docs/finals-report-plan.md) | 四份报告的字段、结构与本页的取舍 |
| 项目进展演示稿 | [`docs/progress-presentation.pptx`](https://github.com/niucurry/oskernel_agent/blob/main/docs/progress-presentation.pptx) | 阶段性汇报用演示稿 |

## 源码入口

| 模块 | 位置 | 职责 |
|---|---|---|
| 命令入口 | `src/oskernel_agent/cli/` | 正式命令与单步报告函数库 |
| 对比分析 | `src/oskernel_agent/comparison/` | 历史入库、召回、精确比对与对比报告 |
| 描述报告 | `src/oskernel_agent/pipeline/` | 源码事实抽取与描述报告流水线 |
| 开发过程报告 | `src/oskernel_agent/finals/` | 开发过程报告、摘要模型与清理门禁 |
| 前端控制台 | `frontend/` | Vue 3 控制台与 Node API |
| 测试 | `tests/` | 单元、集成与召回评测回归 |

## 仓库

- GitHub：<https://github.com/niucurry/oskernel_agent>
- 许可证：MIT
