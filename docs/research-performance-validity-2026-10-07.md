# 性能评审的实际故障验证与候选停止

本轮在新目录实现并执行了[原版性能基准适用性原型](../research/performance_validity_probe/README.md)。当前可见真实需求包括参赛作者展示 lmbench 排名，生产系统也应避免将一个数值当成功实现的证据；作者公开材料不是独立正式评分真值。本轮先读取原始论文、官方实现和 Linux 系统调用文档，没有把更换评测器或模型当创新。

[Does Systems Research Measure Up? 原论文](https://cs.uwaterloo.ca/~brecht/courses/854-Experimental-Performance-Evaluation-2018/readings/f17/Does-Systems-Research-Measure-Up.pdf)已讨论简单系统调用测量的歧义；[lmbench](https://github.com/intel/lmbench)和[UnixBench](https://github.com/kdlucas/byte-unixbench)的源码与原结果解析器是必要强基线。这里只问未修改原程序在实际 ENOSYS 下是否仍给可解析正指标，是否存在超出普通 POSIX 预检的增益；不宣称这个一般问题新颖。

两个固定上游头、五个语义 workload、两个空调用范围对照、三条件、重复、15 秒/2 MiB 上限均在测量前冻结。实际执行 36 个基准程序单元、21 个独立 strace 系统调用见证；原源码和计时库未改。20 单元完成，16 未知。UnixBench 的 close/dup 两次无诊断地计入失败 dup 操作；exec 两次也有数值，但其原解析器能识别错误。原诊断检查留下两个受控语义误计数记录，普通 POSIX 预检留下零个。空调用故障计数仅是分派路径，不能当功能缺陷。

lmbench 有一个放行写对照完成，14 单元超时；管道两个故障单元因输出上限终止。未知仍在原分母中，不能写为阴性。初始解析器重放适配器错误插入空行并误用 ERROR 真假值，原材料完整保留；另冻结纠正协议后只重放相同捕获字节，没有重跑任何基准。最终原版解析器检查是否定义 ERROR，四个/两个/零的语义结果不变。

结论：停止“简单预检加错误日志门控”的新算法主张，没有额外收益证据；跨两套件冻结 H1 未获支持，但 lmbench 未就绪意味着不能宣称无预算限制现象已被证伪，也不能否定整个性能评审实证领域。没有真实比赛内核自然缺陷、独立 OS 数量、排名变化或顶刊效果验证。基准墙钟合计 231.45697 秒、子 CPU 231.004897 秒；见证、重放、构建和资源操作另计。

同轮完成[可恢复缓存压缩](../research/preparation_cache_compaction/README.md)，458 文件逐内容核对，净回收约 175.09 MiB，可保留工程收益。随后按新资源协议进行[一次 NoAxiom 内核准备](../research/noaxiom_boot_feasibility/README.md)，约 5 秒触发磁盘底线，无 ELF/启动，仍为准备未知；不继续降底线或换 profile 重试。已有生产修复、用户删除和负结果未改变，未重跑旧条件/压缩实验，也未把恢复控制或构建成功当论文效果。总体研究目标仍未达到，保持进行中。
