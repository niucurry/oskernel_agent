"""OS 内核作品查重引擎（feature/clone-detection-engine）。

四层漏斗：simhash 粗筛 → embed 向量召回 → segment 分段验证 → exact 精确比对，
辅以 ingest 数据获取、normalize 归一化、metadata 元数据信号、review LLM 复核、report 报告生成。

每个子模块都提供独立 CLI 入口（python -m src.<module>），中间产物落盘解耦。
"""
