name: innovation-fs
description: 文件系统创新性分析（VFS·inode/块缓存/崩溃一致性·日志/索引与布局 的「教学基线→工程实践→前沿」与 1–5 创新度锚点；仅分析「文件系统」子系统时加载）
applies_to: subsys
<!-- body -->
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【技能：文件系统（fs）子系统创新性分析】

**触发条件：** 仅当本子系统是「文件系统」时加载。用本卡梯度在**子系统总览**里写一节
「创新性分析」（不写进模块详细）。判定**只能引用本卡内容**；无对应机制或与教学基线一致 →
定 1–2 分并写「与标准做法一致 / 无法对标前沿，存疑」，**禁止编造论文/格式名**。带 file:line。

━ VFS 抽象 ━
- 基线(1分)：单一文件系统直连，无统一抽象；或 easy-fs 式固定层次。rCore easy-fs / xv6 fs 常规。
- 工程(2–3分)：VFS trait/ops 统一接口、多 fs 挂载、路径解析与 dentry 缓存、设备文件/管道接入 VFS。
- 前沿(4–5分)：可堆叠/联合文件系统(overlayfs)、FUSE 式用户态 fs、fd 抽象统一 socket/pipe/file。

━ inode / 索引 / 磁盘布局 ━
- 基线(1分)：直接块 + 一级间接块、bitmap 管理空闲块（easy-fs 风格）。
- 工程(2–3分)：多级间接 / extent 区段映射、inode 缓存、目录哈希索引、超级块 + 块组。
- 前沿(4–5分)：B-tree/B+tree 索引(类 btrfs/XFS)、写时复制 fs、log-structured 布局(LFS)。

━ 块缓存 / page cache ━
- 基线(1分)：固定大小 buffer 数组 + 顺序扫描，简单替换。
- 工程(2–3分)：LRU/时钟替换、读写分离、预读(readahead)、统一 page cache。
- 前沿(4–5分)：自适应替换(ARC)、与 mmap 统一的 page cache、io_uring 异步 IO 路径。

━ 崩溃一致性 / 日志 ━
- 基线(1分)：无崩溃保护，直接写盘。
- 工程(3分)：事务式 write-ahead log / journaling（有 begin/commit/recover，参考 xv6 log）、
  fsync 屏障。有完整提交+恢复路径才给 3。
- 前沿(4–5分)：CoW 快照一致性、soft updates、校验和(checksum)+自愈、崩溃一致性形式化验证。

【创新度判定提示】整段取最高对位项。看信号：trait/ops 抽象、extent/B-tree、缓存替换策略、
事务日志的 commit/recover、CoW/快照、checksum。仅「改了块大小常量」不加分。
