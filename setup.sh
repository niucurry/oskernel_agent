#!/usr/bin/env bash
# 项目环境一键安装脚本（Ubuntu 24.04 / Debian）
set -e

# ── 系统工具 ──────────────────────────────────────────────────────────────────
echo "[1/4] 安装系统工具..."
sudo apt-get update -qq
sudo apt-get install -y \
    python3 python3-pip python3-venv \
    universal-ctags \
    clangd \
    bear \
    curl \
    git

# ── rust-analyzer ─────────────────────────────────────────────────────────────
echo "[2/4] 安装 rust-analyzer..."
if ! command -v rust-analyzer &>/dev/null; then
    RUST_ANALYZER_URL="https://github.com/rust-lang/rust-analyzer/releases/latest/download/rust-analyzer-x86_64-unknown-linux-gnu.gz"
    curl -fL "$RUST_ANALYZER_URL" | gunzip -c > /tmp/rust-analyzer
    chmod +x /tmp/rust-analyzer
    sudo mv /tmp/rust-analyzer /usr/local/bin/rust-analyzer
    echo "  rust-analyzer 安装完成：$(rust-analyzer --version)"
else
    echo "  rust-analyzer 已存在：$(rust-analyzer --version)"
fi

# ── Python 虚拟环境 ───────────────────────────────────────────────────────────
echo "[3/4] 创建虚拟环境并安装 Python 依赖..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 -m venv "$SCRIPT_DIR/.venv"
"$SCRIPT_DIR/.venv/bin/pip" install --upgrade pip -q
"$SCRIPT_DIR/.venv/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"

# ── 验证 ──────────────────────────────────────────────────────────────────────
echo "[4/4] 验证安装..."
check() {
    if command -v "$1" &>/dev/null; then
        echo "  [OK] $1: $($1 --version 2>&1 | head -1)"
    else
        echo "  [缺失] $1: 未找到"
    fi
}
check clangd
check bear
check rust-analyzer

# ctags 单独验证：必须是 Universal Ctags（支持 --output-format=json）
if command -v ctags &>/dev/null; then
    CTAGS_VER=$(ctags --version 2>&1 | head -1)
    if echo "$CTAGS_VER" | grep -qi "universal"; then
        echo "  [OK] ctags (Universal): $CTAGS_VER"
    else
        echo "  [警告] ctags: 检测到非 Universal Ctags（$CTAGS_VER）"
        echo "    本项目需要 Universal Ctags，请运行："
        echo "      sudo apt-get install -y universal-ctags"
        echo "    若已安装但命令仍指向旧版，检查 PATH 中的优先级："
        echo "      which -a ctags"
    fi
else
    echo "  [缺失] ctags: 未找到"
fi

"$SCRIPT_DIR/.venv/bin/python" - <<'EOF'
import importlib, sys
pkgs = ["openai", "git", "tree_sitter", "tree_sitter_c", "tree_sitter_rust"]
for p in pkgs:
    try:
        importlib.import_module(p)
        print(f"  [OK] {p}")
    except ImportError:
        print(f"  [缺失] {p}: 未安装")
EOF

echo ""
echo "完成！激活虚拟环境后运行项目："
echo "  source .venv/bin/activate"
echo "  python agent.py"
