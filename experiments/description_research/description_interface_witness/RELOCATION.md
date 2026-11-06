# 位置变更

该失败原型在 2026-10-08 按新的交接要求从生产仓库 research/ 移至 /home/niu/agent-research/description_interface_witness/。原已校验归档保持不变，见 preservation.json。

旧 README 中的 research/description_interface_witness/ 路径现应替换为本绝对路径；脚本应从生产仓库 /home/niu/agent 设置 PYTHONPATH=src 运行，或将 /home/niu/agent/src 加入 PYTHONPATH。不为迁移改写冻结的历史原始文件与模型输出。

后来的 source_inventory 控制关键字修复会使旧 freeze 中对应生产文件的当前 SHA 与历史不同。这是后续工程变更，历史冻结与归档中的原生产快照仍可核对；不重写历史 freeze 来制造“当前零变化”记录。
