# OS 内核代码分析 Agent

对学生提交的操作系统内核代码进行**完整性与原创性自动评估**的 AI Agent。

---

## 目录结构

```
agent/
├── agent.py              # 主入口：分析单仓库或比较两个仓库
├── fetch_single_repo.py  # 克隆远程仓库并生成元数据
├── config.toml           # 配置文件（API Key、路径、引擎参数）
├── config.py             # 读取 config.toml
├── prompts.py            # 分层提示词构建
├── requirements.txt      # Python 依赖
├── setup.sh              # 一键环境安装脚本
├── engines/
│   ├── base.py           # 引擎抽象接口
│   ├── lsp_base.py       # LSP 客户端基类
│   ├── path_a.py         # 引擎A：rust-analyzer（Rust 项目，精度最高）
│   ├── path_b.py         # 引擎B：clangd（C 项目）
│   └── path_c.py         # 引擎C：tree-sitter（降级方案，无需 LSP）
├── parser/
│   ├── code_parser.py    # 符号提取、结构分析（依赖 ctags）
│   └── os_tools.py       # 两级索引与仓库地图构建
├── tools/
│   ├── mcp_tools.py      # 工具统一入口
│   ├── tool_registry.py  # 工具 Schema 定义
│   ├── tool_dispatcher.py # 工具调度与引擎聚合
│   ├── tool_handlers.py  # 底层实现（文件读取、syscall 扫描）
│   └── reference_db.py   # 相似度指纹数据库
└── scripts/
    └── build_reference_db.py  # 构建参考 OS 指纹库
```

---

## 一、环境安装

### 一键安装（推荐）

```bash
bash setup.sh
source .venv/bin/activate
```

脚本自动安装：`universal-ctags`、`clangd`、`bear`、`rust-analyzer` 以及 Python 虚拟环境。

### 手动安装

```bash
sudo apt-get install -y universal-ctags clangd bear

curl -fL https://github.com/rust-lang/rust-analyzer/releases/latest/download/rust-analyzer-x86_64-unknown-linux-gnu.gz \
  | gunzip -c | sudo tee /usr/local/bin/rust-analyzer > /dev/null
sudo chmod +x /usr/local/bin/rust-analyzer

pip install -r requirements.txt
```

---

## 二、配置

编辑 `config.toml`（git-ignored，含 API Key）：

```toml
[api]
key        = "sk-..."                       # DeepSeek 或 OpenAI 兼容接口的密钥
base_url   = "https://api.deepseek.com/v1"
model      = "deepseek-chat"
temperature = 0.1

[data]
repos_dir    = "./data/historical_repos"   # 克隆下来的仓库存放目录
metadata_dir = "./data/metadata"

[target]
repo_id = "T202510008995695-2259"          # 默认分析的仓库（可被命令行参数覆盖）

[engine]
rust_analyzer_timeout = 120   # 等待 rust-analyzer 索引完成的秒数
clangd_timeout        = 60    # 等待 clangd 索引完成的秒数
max_call_depth        = 3     # get_call_chain 默认展开层数
skip_dirs = ["vendor", "third_party", "target"]
```

---

## 三、使用方法

所有仓库参数均可通过命令行直接指定，无需修改代码或配置文件。

### 分析单个仓库

```bash
# 使用 config.toml 中的默认 repo_id
python agent.py

# 指定已克隆的仓库名（data/historical_repos/ 下的文件夹名）
python agent.py --repo-id T202510008995695-2259

# 指定本地仓库的完整路径
python agent.py --repo-path /path/to/repo

# 直接给 URL，自动克隆后分析（不需要手动 fetch）
python agent.py --url https://gitlab.eduxiji.net/.../repo.git
```

### 保存报告

```bash
python agent.py --repo-id REPO_NAME --output report.md
# 或者重定向（包含所有日志）
python agent.py > report.txt 2>&1
```

### 覆盖模型

```bash
python agent.py --repo-id REPO_NAME --model deepseek-chat
```

### 比较两个仓库

```bash
# 比较两个已克隆的仓库
python agent.py --compare --repo-id REPO_A --repo-id-b REPO_B

# 比较两个远程仓库（自动克隆）
python agent.py --compare --url URL_A --url-b URL_B

# 混合：本地 + 远程
python agent.py --compare --repo-path /path/to/a --url-b URL_B
```

### 只克隆仓库（不分析）

```bash
python fetch_single_repo.py https://gitlab.eduxiji.net/.../repo.git

# 自定义存放目录
python fetch_single_repo.py https://... --output-dir ./data/historical_repos
```

### 构建参考指纹库（可选，用于原创性检测）

```bash
python scripts/build_reference_db.py --reference rcore-tutorial-v3 --repo-path /path/to/rCore-Tutorial-v3
```

---

## 四、命令行参数完整说明

### `agent.py`

| 参数 | 说明 |
|------|------|
| `--repo-id ID` | 分析 `data/historical_repos/` 下的指定仓库 |
| `--repo-path PATH` | 分析任意本地路径下的仓库 |
| `--url URL` | 克隆远程仓库后分析 |
| `--output FILE` / `-o FILE` | 将报告写入文件（默认打印到终端） |
| `--model MODEL` | 覆盖 config.toml 中的模型名称 |
| `--compare` | 启用比较模式 |
| `--repo-id-b ID` | 比较模式：第二个仓库的文件夹名 |
| `--repo-path-b PATH` | 比较模式：第二个仓库的本地路径 |
| `--url-b URL` | 比较模式：第二个仓库的远程地址 |

`--repo-id` / `--repo-path` / `--url` 三者互斥，`--repo-id-b` / `--repo-path-b` / `--url-b` 同理。

### `fetch_single_repo.py`

| 参数 | 说明 |
|------|------|
| `url`（位置参数） | 仓库 HTTPS 地址 |
| `--output-dir DIR` | 本地存放目录（默认 `./data/historical_repos`） |
| `--meta-dir DIR` | 元数据目录（默认 `./data/metadata`） |
