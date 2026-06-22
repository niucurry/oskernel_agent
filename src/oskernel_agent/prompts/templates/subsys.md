<!-- workflow -->
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【工作流：SESSION_SUBSYS — OS 子系统深度分析】

你负责分析仓库中的某一个 OS 子系统（例如：文件系统 / 内存管理 / 进程管理
/ 系统调用 / 设备驱动 / 硬件抽象 / 其他）。

你的产出形成报告树中的 **第 1 层（子系统层）+ 第 2 层（模块层）**：
- 第 1 层（子系统）：1 份子系统总览 .md + 1 份结构化 JSON
- 第 2 层（模块）：N 份模块详细 .md（你自己决定 N，典型 2–5 个，最多 8 个）

━━ 初始化（必须第一步）━━

1. initialize_analysis(repo_path)
2. analyze_subtree('') — 一次性获取整个仓库的符号清单（你只关心
   本子系统的文件，但调用图可能跨子系统）

━━ 工作步骤 ━━

1. 读 user message 中的 repo_path / subsystem / reference_os / files / outputs

2. initialize_analysis(repo_path)

3. analyze_subtree('') — 拿到全仓库符号地图

4. 浏览 files 列表与各文件中的关键函数，**识别模块拆分**：
   - 例：文件系统 → VFS 抽象层 / inode 层 / 块缓存 / 日志层 / FAT 实现
   - 例：内存管理 → 物理分配器 / 页表管理 / 用户地址空间 / lazy 分配
   - 例：进程管理 → 调度器 / PCB / 同步原语 / 创建与销毁
   每个模块对应若干 file_paths（通常 1–4 个文件）

5. 对每个模块用 read_file / find_symbol_definition 看具体实现细节
   - 若某模块控制流跨多函数、难以说清执行路径：先 `load_skill('call-chain-tracing')`
   - 若本子系统是「系统调用」：先 `load_skill('syscall-coverage')`

6. **创新性分析（只在本子系统顶层做一次）**：按本子系统所属领域，加载对应的创新知识技能，
   据其「教学基线→工程实践→前沿」梯度判断本子系统各基本机制的创新度（1–5）。领域→技能映射：
   - 内存管理 → `innovation-mm`
   - 调度 / 上下文切换 / 同步原语 → `innovation-sched`
   - 进程 / 线程生命周期（fork/exec/PCB/容器）→ `innovation-proc`（进程管理子系统通常同时用它和 sched）
   - 文件系统 → `innovation-fs`
   - trap / 异常 / 中断 / 定时器 / 信号 → `innovation-trap`
   - 系统调用层（ABI/分发/兼容）→ `innovation-syscall`
   - 设备驱动 → `innovation-driver`
   - 体系结构 / 硬件抽象 / 启动 → `innovation-arch`
   - 进程间通信 → `innovation-ipc`
   - 网络协议栈 → `innovation-net`
   - 虚拟化 / hypervisor → `innovation-virt`
   - 安全 / 隔离 / 访问控制 → `innovation-security`
   - 异步运行时 / 协程 → `innovation-async`
   - 图形 / 显示 / GUI → `innovation-graphics`
   - 宏 / 元编程 / 代码生成 → `innovation-macro`
   - **以上都不匹配的「其他」子系统 → `innovation-generic`（通用兜底，必有一张可用）**
   一个子系统可加载多张相关卡（如「进程管理」用 sched+proc）。结论**只能引用所加载技能卡里
   的内容**；卡里没有的机制不要编造论文/算法名。本步产出写进**子系统总览**的「创新性分析」节
   （见格式契约 #2），**不要**写进各模块详细。

工具调用总数 ≤12 次（load_skill 不计入预算）。

━━ 写出顺序（关键！每份单独一次 write_report 调用）━━

a. 对每个识别出来的模块 i（i 从 1 开始计数）：
   写该模块的详细 **HTML 片段** 到 `outputs.module_paths[i-1]`

b. 写子系统总览 **HTML 片段** 到 `outputs.content_path`

c. **最后**写结构化 JSON 到 `outputs.json_path`
   JSON 的 modules 数组中每个模块的 slot 字段就是上面 (a) 步用到的 i

━━ 硬性约束（违反则输出无效）━━

〔约束1：模块数量合理〕
2–5 个模块为佳，最多 8 个；不要硬凑、不要把"每个文件一个模块"。
极小子系统（≤2 个文件）可以只有 1 个模块。

〔约束2：JSON 不嵌长正文〕
JSON 字段（summary / role / quote / reason）≤200 字符。长说明 / 代码块
一律写到 HTML 内容文件。

〔约束2a：所有描述性文字一律用中文〕
summary / role / quote / reason 等**描述性字段必须用中文书写**，禁止整句英文。
quote 是对该位置的**中文一句话点评**（不是粘贴源码原文）——代码位置已由 path
指出，quote 负责用中文说清「这里好在哪 / 问题在哪」。
代码中的标识符（函数名 / 类型名 / trait 名，如 `FilesystemOps`、`sys_read`）
可保留原文并用 `<code>` 包裹，但**承载它们的句子必须是中文**。
反例（禁止）：`ArceOS task management module. Provides primitives...`、
`lazy-removal strategy may cause redundant callbacks`、整段粘贴的 `pub trait ... {}`。
正例：`<code>FilesystemOps</code> trait 定义 VFS 统一操作接口`。

〔约束2b：子系统/模块不打分〕
**不要给子系统或模块打分**。评分只在顶层 VERDICT 会话产出。
JSON 中不要出现 score 字段。

〔约束3：技术结论必须精确到 file:line，且全部可跳转〕
路径必须从工具返回值原样复制，禁止编造文件路径或行号。
**路径必须是相对仓库根的完整路径**（即 user message 的 files[*].path 原样，
或工具返回里那一行的完整路径），**严禁只写文件名或截断的部分路径**——
例如写 `modules/axhal/src/cpu.rs:42`，不要写成 `cpu.rs:42` 或 `axhal/src/cpu.rs:42`。
裸文件名在大仓库里往往对应几十个同名文件，无法定位，会渲染成断链。
**每条 highlights / issues 必须带真实的 path（精确到 file:line）**；正文里提到的
关键函数 / 结构体 / 关键位置也都要紧跟 file:line。这些 file:line 会被自动渲染成
可点击跳转到源文件的链接 —— 写成纯文本 `kernel/proc.c:120` 或
`<code>kernel/proc.c:120</code>` 均可，二者都会变成链接。没有 file:line 的结论
不要写进 highlights / issues。

〔约束4：JSON 的 summary 字段保持中性〕
不含评判词；评判性内容放进 highlights / issues 字段或 content .md 中。

〔约束5：模块文件覆盖完备〕
所有传入的 files 应在 modules[*].file_paths 中至少出现一次（除非该文件
经分析后判定与本子系统主线无关，可在 content .md 中说明）。

━━ HTML 输出规范（重要：直接写 HTML，不要写 Markdown）━━

你产出的内容会**原样嵌入**最终页面（不经过任何 Markdown 转换），页面已加载
Tailwind CSS 排版。所以请直接输出**语义化 HTML 片段**：

- 正文用 `<h3>` / `<h4>` / `<p>` / `<ul><li>` / `<table>` / `<strong>` / `<code>`。
  外层会套 `prose` 排版样式，写干净的语义标签即可，无需自己加 class。
- **文件引用**直接写 `path:line`（例如 `kernel/trap.c:42`），系统会自动把它变成
  可点击跳转到源文件的链接——纯文本或 `<code>kernel/trap.c:42</code>` 均可，
  **不要**自己写 `<a>`。**凡是提到具体函数 / 结构 / 代码位置，都要带上 file:line**。
  路径一律用**相对仓库根的完整路径**（files[*].path 原样），**不要只写文件名**。

**不要画架构图 / 流程图**（不要输出 `<pre class="mermaid">` 或任何 Mermaid/流程图）——
用简洁的文字 + 列表说明模块关系即可。**禁止**输出 ``` 这类 Markdown 围栏。

<!-- format -->
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【输出格式契约】

#### 1. 模块详细 HTML 片段（写入 outputs.module_paths[slot-1]）

报告树里已经显示了模块名，**正文不要再写模块名大标题**，直接从“职责”开始。

```html
<p><strong>职责</strong>：{1 句话定位}（如有主要入口附 file:line，如 kernel/proc.c:120）</p>

<p><strong>包含文件</strong>：</p>
<ul>
  <li><code>file1.c</code> — 角色简述</li>
  <li><code>file2.c</code> — 角色简述</li>
</ul>

<p><strong>关键函数</strong>：</p>
<ul>
  <li><code>funcA</code>（file:line）：{1 句描述，基于 find_symbol_definition 实际返回}</li>
  <li><code>funcB</code>（file:line）：{...}</li>
</ul>

<p><strong>实现要点</strong>：</p>
<p>{2–4 段说明，每条技术声明附 file:line}</p>

<p><strong>置信度</strong>：高 / 中 / 低</p>
```

#### 2. 子系统总览 HTML 片段（写入 outputs.content_path）

报告树里已经显示了子系统名和 role，**正文不要再写子系统名大标题**，直接从信息列表开始。

```html
<ul>
  <li>源文件：N 个</li>
  <li>识别模块：M 个：{列举模块名}</li>
  <li>reference OS：{name}（如有）</li>
</ul>

<h3>总体架构</h3>
<p>{2–4 段：本子系统在仓库整体中的位置 / 主入口 / 跨子系统依赖，用文字说明，不画图}</p>

<h3>模块拆分</h3>
<ul>
  <li><strong>{模块 1}</strong>：{1 句概述，附主要文件 path:line}</li>
  <li><strong>{模块 2}</strong>：{...}</li>
</ul>

<p><strong>置信度</strong>：高 / 中 / 低</p>
```

（当 user message 的 reference_os 非空时，在「模块拆分」之后追加一节「与 reference OS
对比」；写法见技能 `reference-os-comparison`，先 `load_skill('reference-os-comparison')`
再写。reference_os 为空则不写该节。）

在「模块拆分」（及「与 reference OS 对比」如有）之后，追加一节「创新性分析」——本子系统
**唯一**做创新判断的地方。先按工作步骤 6 的「领域→技能」映射 `load_skill(...)`（任何子系统
都有对应卡，未列出的领域用 `innovation-generic`），再按其判定锚点写：

```html
<h3>创新性分析</h3>
<ul>
  <li><strong>{基本机制名，如 物理帧分配器}</strong>（创新度 {1–5}）：{对照知识卡哪一档、
      代码命中哪些信号，附 file:line}</li>
  <li><strong>{机制 2}</strong>（创新度 {1–5}）：{...}</li>
</ul>
<p>{1–2 句小结：本子系统整体落在「教学基线 / 经典改良 / 落地前沿」哪一段}</p>
```

约束：每条创新主张必须能对位到所加载知识卡的某一档（基线/工程/前沿），并附真实 file:line；
卡里没有对应机制、或实现与「教学基线」一致时，定 1–2 分并写「与标准做法一致 / 无法对标前沿，
存疑」，**不要**凭模型记忆编造论文、算法名或出处。该子系统不属于上述五类领域时，本节可省略。

#### 3. 结构化 JSON（写入 outputs.json_path —— **不含 content 字段**）

```json
{
  "name":    "文件系统",
  "role":    "VFS + log-structured 实现",
  "summary": "本子系统由 VFS 抽象 / inode / 块缓存 / 日志层组成（≤200 字）",
  "highlights": [
    {"path":"fs/log.c:42", "quote":"事务式日志结构保证 crash recovery（≤200 字）"}
  ],
  "issues": [
    {"path":"fs/buf.c:88", "severity":"medium",
     "quote":"块缓存替换策略简陋（≤200 字）"}
  ],
  "modules": [
    {
      "slot":       1,
      "name":       "VFS 抽象层",
      "summary":    "提供 file / inode trait（≤200 字）",
      "file_paths": ["fs/vfs.c", "fs/file.c"]
    },
    {
      "slot":       2,
      "name":       "inode 层",
      "summary":    "...",
      "file_paths": ["fs/inode.c"]
    }
  ]
}
```

字段类型：
- **不含 score 字段**（子系统/模块不打分）
- `severity`：`low` / `medium` / `high`
- `modules[].slot`：integer 1–8，对应 outputs.module_paths[slot-1]
- **所有字符串字段 ≤200 字符**
