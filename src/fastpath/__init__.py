"""L0 文件指纹快速通道（fast path）。

在 ingest 后、recall 前运行：用整文件规范化哈希检测新作品中与历史库**逐文件相同**
（仅空格/格式/注释差异）的整文件复制。命中文件直接定案为「文件整体相同」，并在 recall
阶段跳过其全部函数的嵌入与检索——同时补漏（整文件复制必报，回应 D3）+ 提速（命中文件
跳过最贵的嵌入步骤，回应 P1）。
"""

from .scan import aggregate_file_similarity, scan_repo

__all__ = ["scan_repo", "aggregate_file_similarity"]
