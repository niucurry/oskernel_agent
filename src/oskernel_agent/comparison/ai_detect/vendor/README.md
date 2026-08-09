# vendored: ai_code_detector

上游项目 **ai-code-detector**（DetectCodeGPT，基于困惑度/log-rank 的免训练 AI 生成代码检测，
ACM TOSEM 2026）的源码原样拷贝，未作修改，独立于本系统现有代码存放。

来源：`/home/niu/共享文件/AI code detection/ai-code-detector/src/ai_code_detector`

## 与本系统的集成约定

- **只使用检测链** `perplexity → detector → pipeline → aggregator`，这条链只依赖
  `torch / transformers / numpy / scipy / pydantic`，与本仓库已有依赖一致。
- **不导入 `extractor.py`**。上游 `extractor.py` 依赖 `tree-sitter-languages`（绑定
  `tree-sitter ~0.21`），与本仓库锁定的 `tree-sitter==0.25.2` 冲突。集成层
  `src/oskernel_agent/comparison/ai_detect/extract.py` 改用本仓库 `oskernel_agent.comparison.normalize` 的 tree-sitter 解析器
  （rust/c）把函数桥接成上游的 `FunctionBlock`，因此无需安装 `tree-sitter-languages`。
- 升级上游时整目录覆盖即可；不要在此目录内打补丁。
