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

### Linux

#### 一键安装（推荐）

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

#### 手动安装

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

### Windows

#### 第 0 步：基础环境准备（如果已有可跳过）

在开始之前，请确保你的电脑上已经安装了以下四款基础软件。如果没有，请点击下方链接去官网下载并默认安装（**注意：安装 Python 时，请务必勾选“Add Python.exe to PATH”**）：

1. **[Visual Studio Code](https://code.visualstudio.com/)**：我们的主力编辑器。
2. **[Python 3.x](https://www.python.org/downloads/)**：运行脚本的基础环境。
3. **[Git for Windows](https://git-scm.com/download/win)**：用于拉取代码仓库。
4. **[Rust 工具链](https://rustup.rs/)**：下载 `rustup-init.exe` 并运行，按照默认提示按 `1` 安装即可（用于解析 Rust 代码）。

---

#### 第 1 步：安装 VS Code 必备插件

打开 VS Code，点击左侧导航栏的 **“扩展”** 图标（或者按下快捷键 `Ctrl + Shift + X`），在搜索框中依次搜索并安装以下四款插件：

* **`Python`**（发布者：Microsoft）- 提供 Python 运行支持。
* **`C/C++`**（发布者：Microsoft）**或者** **`clangd`**（发布者：LLVM）- 二选一即可，用于提供 C/C++ 代码的跳转和解析。
* **`rust-analyzer`**（发布者：The Rust Programming Language）- 安装后它会在右下角提示下载语言服务器，点击允许即可。
* **`CMake Tools`**（发布者：Microsoft）- 关键插件！在 Windows 上我们将用它来替代 Linux 中的 `bear` 工具。

---

#### 第 2 步：安装全局环境依赖（关键）

虽然 VS Code 插件很强大，但我们的 Python 后台脚本仍然需要能够直接调用某些系统命令。我们需要把它们安装到系统全局。

1. 点击电脑左下角的“开始”菜单，搜索 **PowerShell**，右键选择**“以管理员身份运行”**。
2. **安装 universal-ctags**：在弹出的蓝底窗口中，复制粘贴以下命令并回车：
   ```powershell
   winget install UniversalCtags.UniversalCtags
   ```

*(如果提示是否同意协议，输入 `Y` 并回车)*
3. **安装 rust-analyzer 全局组件**：继续在 PowerShell 中输入以下命令并回车：

```powershell
   rustup component add rust-analyzer
```

---

### 第 3 步：配置 CMake 以生成 `compile_commands.json`

Linux 系统通常使用 `bear` 来拦截编译过程并生成 `compile_commands.json`（C/C++ 代码解析必须的文件），但 Windows 不支持 `bear`。我们通过 CMake 插件来完美替代：

1. 回到 VS Code，按下快捷键 `Ctrl + ,`（逗号）打开**设置**界面。
2. 在顶部的搜索框中输入：`CMake: Export Compile Commands`。
3. 在搜索结果中，找到对应的选项并**打上勾**。
4. **如何生效**：当你用 VS Code 打开你的 C/C++ 项目，并在底部状态栏选择好编译器（Kit）后，CMake Tools 会自动进行配置（Configure），此时它就会默默在项目的 `build` 文件夹下为你生成 `compile_commands.json` 文件。

---

### 第 4 步：初始化 Python 虚拟环境

最后一步，我们需要隔离 Python 的依赖包，防止弄乱你电脑原本的 Python 环境。

1. 在 VS Code 中打开你的项目文件夹。
2. 点击顶部菜单栏的 **“终端(Terminal)”** -> **“新建终端(New Terminal)”**（或者按 `Ctrl + \``）。
3. 确保终端类型是 PowerShell，然后依次逐行执行以下命令：

```powershell
# 1. 创建一个名为 .venv 的虚拟环境文件夹
python -m venv .venv

# 2. 激活这个虚拟环境（你会看到命令行前面多了一个绿色的 (.venv) 标识）
.\.venv\Scripts\activate

# 3. 安装项目所需的所有依赖包（需确保项目根目录下有 requirements.txt）
pip install -r requirements.txt
```

> **💡 小贴士**：如果由于系统安全策略导致第二条命令（激活虚拟环境）报错，请在**管理员权限**的 PowerShell 中执行一次 `Set-ExecutionPolicy RemoteSigned`，输入 `Y` 确认，然后重启 VS Code 再次尝试激活。

🎉 **至此，Windows 环境已彻底搭建完毕，可以继续进行后续的配置和使用了！**

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

| 优先级      | 引擎          | 适用场景                                 | 精度 |
| ----------- | ------------- | ---------------------------------------- | ---- |
| A（最优先） | rust-analyzer | 仓库含 `Cargo.toml`（Rust 项目）       | 高   |
| B           | clangd        | C 项目，能生成 `compile_commands.json` | 高   |
| C（降级）   | tree-sitter   | 其他情况，无需 LSP 环境                  | 中   |

若 rust-analyzer 或 clangd 未安装，自动降级到 tree-sitter。

---

## 五、可用工具（Agent 内部调用）

Agent 在分析过程中会自动调用以下工具，无需手动操作：

| 工具                                         | 说明                       |
| -------------------------------------------- | -------------------------- |
| `get_call_chain(function_name, max_depth)` | 从入口函数展开调用树       |
| `get_struct_fields(struct_name)`           | 获取结构体完整字段列表     |
| `find_references(symbol_name)`             | 查找所有调用该符号的位置   |
| `go_to_definition(symbol_name)`            | 查找符号定义，返回完整源码 |

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
