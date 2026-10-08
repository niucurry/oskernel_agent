# 继续选题：性能试验已关闭，真实缺陷真值调查已开始

总目标 active，尚未验证出真正合适的论文方法。用户要求持续实际开发实验，兼顾生产与 TSE/TOSEM；三条禁止的旧摘要/条件主线继续禁止。先前完整交接及 [历史/来源交接](research-handoff-history-and-provenance-2026-10-07.md)全部保持，不能重做被关闭候选来求正结果。没有 subagent 或新模型/提示/角色变体。

## 本轮完成与停止

- [性能适用性原型](research-performance-validity-2026-10-07.md)，`research/performance_validity_probe`：两个原版固定提交、原源码与计时库未改；36 次主机基准程序单元，20 完成、16 未知（14 超时、2 输出上限）。四个 UnixBench 受控语义误计数：dup 两次无诊断，exec 两次有诊断且原解析器已拒绝。弱解析/原诊断/普通 POSIX 预检后为 4/2/0。Null 分派计数不算功能缺陷，不比较时延/排名。跨两套件 H1 未获支持，lmbench 未就绪不能当阴性；简单预检门控没有新算法增益，按协议停止，不改 timeout/iterations/套件/任务救这次试验。
- 21 次独立 strace 见证进程，7 操作×3 条件；实际 ENOSYS 都经原 syscall 和 trace 确认。见证、初始 21 次原解析器重放及纠正后 21 次重放不算主方法单元。构造器写独立 fd 198，无全局内核/sysctl 改动，无模型互评。初始工具重放多插空行并误用 ERROR 真值，原材料保留；最终按原版 defined(ERROR) 检查，只重放相同原 stderr，基准重跑零。最终看 analysis-v2.json，不能只引用初始 controls_all_B0=false 而隐藏九个未知对照。
- 前期 392 单元仍冻结；本轮另计 36 个 Linux 主机性能程序（含未知），不能加见证/编译/恢复控制来膨胀样本或说多个独立 OS。QEMU/NuttX 新启动零。本轮基准 231.4569709 秒墙钟 / 231.004897 秒子 CPU。原 Make 两入口与原型两个直接编译入口，共 14 个实际 GCC 编译/链接命令，不是四个编译器进程。
- [可恢复缓存](../research/preparation_cache_compaction/README.md)：458 文件完整内容校验后仅归档并移除 task-owned inactive NoAxiom 两 target，净回收约 175.09 MiB，原 469 源码/两个用户 ELF/历史负结果不变。restore.py 有 SHA/路径/type/现有目标/空间检查，手动恢复硬链接、mode、文件纳秒时间，renameat2 NOREPLACE 发布；四项小真实恢复控制全通过。真实用户/内核归档只 dry-run，没有恢复。当前用户目标缺空间，内核目标已有本轮停止缓存，均不可直接恢复。
- [NoAxiom 单次新准备](../research/noaxiom_boot_feasibility/README.md)：全局第四次入口、该独立协议一次，原 2024 compiler/profile/features/offline locked。显式工程资源规则从旧双 target/连续512MiB改为无活动用户target、单内核256MiB、初始512MiB/连续256MiB，没有冒充旧预算。4.971655 秒/8.057552 子CPU触发磁盘底线（268,062,720 < 268,435,456 bytes），不是 target 上限；无 ELF、汇编或源码变化，无 boot/method/model。阶段关闭，不能第五次降底线或换 profile 追编译；留为准备未知。

## 保留审计

`research/performance_validity_probe/final-audit.json`：192 份原 stream/trace 校验，408 个该目录材料索引，22 个两工程目录材料索引；独立重核都通过。其 live finalize-run.log 排除以防自哈希。此前 G 273、自然来源 120、来源保护 128 个材料，以及来源生产 8 个 SHA 和 development/test_history SHA 全不变；用户两删除仍保留。该轮只有新增目录、文档和根 README 链接，生产源码零修改，未重跑此前204相关报告回归，不把旧通过数说成本轮测试。没有 cargo/rustc/QEMU 后台进程。结束时余量约242MiB，低于大构建512MiB起始门槛。

## 当前可继续工作

已新建并实际获取 `research/natural_defect_feasibility`：三个固定官方修复 PR（NuttX 16437、16455；Zephyr 109361），六 API/patch 请求成功，77,285 字节、6.801844秒，原响应与 SHA 保留。三补丁各仅改一个生产 C 文件，未含测试文件；NuttX root PR 提及 ostest，rename列两硬件环境，Zephyr关联用户态验证/线程死亡/资源池。没有精确父版本或独立可执行输入/原配置验证；metadata.base.sha 不等于 fix parent。该阶段编译/运行/模型为零，不是方向效果或失败。

新读原始 SoK From Crash to Patch、False-Positive Bug Reports 及作者实现链接；一般增加证据/真实bug数据/报告分类已有直接先例。官方 FreeRTOS2026 MPU/TrustZone 适用公告也读过，但未纳固定样本。下一阶段应先取精确 commit parent 和原回归材料，在独立新协议固定预算、假设、强基线与停止条件后实际验证；不能用猜测的 mocked privilege 或任意宿主行为说 whole-kernel 真值。可用组件级原源码回归必须明确 scope，不等于正式内核或比赛评审效果。不能先把这三例称为独立自然复现成功，也不能将无 test path 推成整个领域没有 oracle。

已向用户问过可选正式参赛材料目录，仍无答复；不重复权限询问，不以沉默当授权/真值，不因此停止所有独立工作。总体目标没有达到完成条件，也未满足无可进展的 blocked 条件。
