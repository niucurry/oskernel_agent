# OS 内核代码分析 Agent

对学生提交的操作系统内核代码进行**完整性与原创性自动评估**的 AI Agent。

---

## 目录结构

```
agent/
├── agent.py              # 主入口，运行分析
├── fetch_single_repo.py  # 克隆仓库并生成元数据
├── config.toml           # 配置文件（API、路径、引擎参数）
├── config.py             # 读取 config.toml
├── requirements.txt      # Python 依赖
├── setup.sh              # 一键环境安装脚本
├── engines/
│   ├── path_a.py         # 引擎A：rust-analyzer（Rust 项目，精度最高）
│   ├── path_b.py         # 引擎B：clangd（C 项目）
│   └── path_c.py         # 引擎C：tree-sitter（降级方案，无需 LSP）
└── parser/
    ├── code_parser.py    # 符号提取、结构分析（依赖 ctags）
    └── os_tools.py       # 仓库地图构建
```

---

## 一、环境安装

### 一键安装（推荐）

```bash
bash setup.sh
```

脚本会自动安装：
- `universal-ctags`：符号提取
- `clangd`：C 代码 LSP 引擎
- `bear`：生成 C 项目的 `compile_commands.json`
- `rust-analyzer`：Rust 代码 LSP 引擎
- Python 虚拟环境及所有依赖包

完成后激活虚拟环境：

```bash
source .venv/bin/activate
```

### 手动安装

```bash
# 系统工具
sudo apt-get install -y universal-ctags clangd bear

# rust-analyzer
curl -fL https://github.com/rust-lang/rust-analyzer/releases/latest/download/rust-analyzer-x86_64-unknown-linux-gnu.gz \
  | gunzip -c | sudo tee /usr/local/bin/rust-analyzer > /dev/null
sudo chmod +x /usr/local/bin/rust-analyzer

# Python 依赖
pip install -r requirements.txt
```

---

## 二、配置

编辑 `config.toml`：

```toml
[api]
key        = "你的 API Key"       # DeepSeek 或 OpenAI 兼容接口的密钥
base_url   = "https://api.deepseek.com/v1"
model      = "deepseek-chat"
temperature = 0.1

[data]
repos_dir    = "./data/historical_repos"   # 克隆下来的仓库存放目录
metadata_dir = "./data/metadata"           # 元数据存放目录

[target]
repo_id = "T202510008995695-2259"          # 要分析的仓库 ID（文件夹名）

[engine]
rust_analyzer_timeout = 120   # 等待 rust-analyzer 索引完成的秒数
clangd_timeout        = 60    # 等待 clangd 索引完成的秒数
max_call_depth        = 3     # get_call_chain 默认展开层数
skip_dirs = ["vendor", "third_party", "target"]  # 跳过的目录（tree-sitter 引擎）
```

---

## 三、使用流程

### 第一步：获取仓库

修改 `fetch_single_repo.py` 顶部的仓库地址：

```python
TARGET_REPO_URL = 'https://gitlab.eduxiji.net/.../你的仓库.git'
```

然后运行：

```bash
python fetch_single_repo.py
```

执行结果：
- 将仓库克隆到 `./data/historical_repos/<仓库名>/`
- 在 `./data/metadata/all_repos_info.json` 中生成元数据（含最近 100 条 commit）

### 第二步：修改配置中的目标仓库

将 `config.toml` 的 `repo_id` 改为刚才克隆的仓库文件夹名：

```toml
[target]
repo_id = "你的仓库名"   # 与 data/historical_repos/ 下的文件夹名一致
```

### 第三步：运行分析

```bash
python agent.py
```

运行过程输出示例：

```
正在执行静态结构分析...
[引擎选择] 路径 A：rust-analyzer
[路径A] rust-analyzer 初始化成功

 Agent 启动（引擎：rust-analyzer，路径A，精度：high）
  [步骤 1] 调用工具：get_call_chain → {'function_name': 'sys_fork', 'max_depth': 3}
  [步骤 2] 调用工具：get_struct_fields → {'struct_name': 'TaskControlBlock'}
  ...

 Agent 分析完毕，输出最终报告：

## 完整性评估
...
## 原创性评估
...
```

---

## 四、分析引擎说明

Agent 按以下优先级自动选择引擎，无需手动指定：

| 优先级 | 引擎 | 适用场景 | 精度 |
|--------|------|----------|------|
| A（最优先） | rust-analyzer | 仓库含 `Cargo.toml`（Rust 项目） | 高 |
| B | clangd | C 项目，能生成 `compile_commands.json` | 高 |
| C（降级） | tree-sitter | 其他情况，无需 LSP 环境 | 中 |

若 rust-analyzer 或 clangd 未安装，自动降级到 tree-sitter。

---

## 五、可用工具（Agent 内部调用）

Agent 在分析过程中会自动调用以下工具，无需手动操作：

| 工具 | 说明 |
|------|------|
| `get_call_chain(function_name, max_depth)` | 从入口函数展开调用树 |
| `get_struct_fields(struct_name)` | 获取结构体完整字段列表 |
| `find_references(symbol_name)` | 查找所有调用该符号的位置 |
| `go_to_definition(symbol_name)` | 查找符号定义，返回完整源码 |

---

## 六、常见问题

**Q：运行时提示 `ctags not found`**  
A：执行 `sudo apt-get install universal-ctags` 后重试。

**Q：路径 A 提示 `rust-analyzer 未安装` 但我已装过**  
A：确认 `rust-analyzer` 在 `$PATH` 中：`which rust-analyzer`。若使用虚拟环境，检查系统 PATH 是否包含 `/usr/local/bin`。

**Q：路径 A 提示 `未找到 Cargo.toml`**  
A：Agent 会自动向子目录递归查找 `Cargo.toml`（跳过 vendor/target），若仓库确实无 Rust 代码则自动降级到路径 B/C。

**Q：分析报告输出后直接退出，没有保存**  
A：目前报告直接打印到终端，可重定向保存：  
```bash
python agent.py > report.txt 2>&1
```

**Q：分析中途出现 `[保护] 注入终止指令`**  
A：Agent 检测到模型在对不存在的符号进行无效猜测（幻觉扩展），已自动打断并要求输出已有结论，属于正常保护机制。
