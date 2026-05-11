# OS 内核代码分析 Agent

对学生提交的操作系统内核代码进行**完整性与原创性自动评估**的 AI Agent。

---

## 一、环境安装

### Linux

#### 前置依赖：OpenCode

本项目使用 [OpenCode](https://opencode.ai) 作为 AI 交互宿主，需要提前安装（要求 Node.js >= 18）：

```bash
npm install -g opencode-ai
```

安装完成后验证：`opencode --version`

#### 一键安装（推荐）

```bash
bash setup.sh
source .venv/bin/activate
```

脚本自动安装：`universal-ctags`、`clangd`、`bear`、`rust-analyzer`、Python 虚拟环境，并将 agent 注册到 OpenCode 全局配置。若 OpenCode 未安装且系统有 npm，脚本会自动安装。

#### 手动安装

```bash
# 安装 OpenCode（需要 npm）
npm install -g opencode-ai

sudo apt-get install -y universal-ctags clangd bear

curl -fL https://github.com/rust-lang/rust-analyzer/releases/latest/download/rust-analyzer-x86_64-unknown-linux-gnu.gz \
  | gunzip -c | sudo tee /usr/local/bin/rust-analyzer > /dev/null
sudo chmod +x /usr/local/bin/rust-analyzer

pip install -r requirements.txt

# 注册 agent 到 OpenCode 全局配置
python setup_opencode.py
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
   winget install --id=UniversalCtags.Ctags -e
   ```

*(如果提示是否同意协议，输入 `Y` 并回车)*
3. **安装 rust-analyzer 全局组件**：继续在 PowerShell 中输入以下命令并回车：

```powershell
   rustup component add rust-analyzer
```

#### 第 3 步：配置 CMake 以生成 `compile_commands.json`

Linux 系统通常使用 `bear` 来拦截编译过程并生成 `compile_commands.json`（C/C++ 代码解析必须的文件），但 Windows 不支持 `bear`。我们通过 CMake 插件来完美替代：

1. 回到 VS Code，按下快捷键 `Ctrl + ,`（逗号）打开**设置**界面。
2. 在顶部的搜索框中输入：`CMake: Export Compile Commands`。
3. 在搜索结果中，找到对应的选项并**打上勾**。
4. **如何生效**：当你用 VS Code 打开你的 C/C++ 项目，并在底部状态栏选择好编译器（Kit）后，CMake Tools 会自动进行配置（Configure），此时它就会默默在项目的 `build` 文件夹下为你生成 `compile_commands.json` 文件。

---

#### 第 4 步：初始化 Python 虚拟环境

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
# 可选：填写后 python agent.py 不带参数时使用此仓库；留空则要求命令行传入参数
repo_id = ""

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
# 指定远程 URL，自动克隆后分析
python agent.py --url https://gitlab.example.com/group/repo.git

# 指定本地仓库的完整路径
python agent.py --repo-path /path/to/repo

# 指定已克隆的仓库名（data/historical_repos/ 下的文件夹名）
python agent.py --repo-id REPO_NAME

# 若在 config.toml [target] 中填写了 repo_id，可不带参数直接运行
python agent.py
```

### 保存报告

```bash
python agent.py --url https://gitlab.example.com/group/repo.git --output report.md
python agent.py --repo-id REPO_NAME --output report.md
# 或者重定向（包含所有日志）
python agent.py --repo-path /path/to/repo > report.txt 2>&1
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

| 参数                            | 说明                                         |
| ------------------------------- | -------------------------------------------- |
| `--repo-id ID`                | 分析 `data/historical_repos/` 下的指定仓库 |
| `--repo-path PATH`            | 分析任意本地路径下的仓库                     |
| `--url URL`                   | 克隆远程仓库后分析                           |
| `--output FILE` / `-o FILE` | 将报告写入文件（默认打印到终端）             |
| `--model MODEL`               | 覆盖 config.toml 中的模型名称                |
| `--compare`                   | 启用比较模式                                 |
| `--repo-id-b ID`              | 比较模式：第二个仓库的文件夹名               |
| `--repo-path-b PATH`          | 比较模式：第二个仓库的本地路径               |
| `--url-b URL`                 | 比较模式：第二个仓库的远程地址               |

`--repo-id` / `--repo-path` / `--url` 三者互斥，`--repo-id-b` / `--repo-path-b` / `--url-b` 同理。

### `fetch_single_repo.py`

| 参数                 | 说明                                             |
| -------------------- | ------------------------------------------------ |
| `url`（位置参数）  | 仓库 HTTPS 地址                                  |
| `--output-dir DIR` | 本地存放目录（默认 `./data/historical_repos`） |
| `--meta-dir DIR`   | 元数据目录（默认 `./data/metadata`）           |
