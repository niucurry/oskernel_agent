#!/usr/bin/env bash
# AutoDL 云端一键部署：在 AutoDL 远程终端、项目根目录下运行  bash deploy_autodl.sh
# 完成后还需手动两步：① 传入 data/db（对比报告必需）② 填 config.toml 的 API key。
set -e
cd "$(dirname "$0")"

# AutoDL 以 root 运行，通常没有 sudo：用同名函数兜底，让 setup.sh 里的 sudo 生效
if ! command -v sudo >/dev/null 2>&1; then sudo() { "$@"; }; export -f sudo; fi

echo "[0/6] 网络加速 + HF 镜像"
[ -f /etc/network_turbo ] && source /etc/network_turbo || true
export HF_ENDPOINT=https://hf-mirror.com

echo "[1/6] Node.js(>=18) + OpenCode（描述报告依赖）"
# apt 的 nodejs 常太老（不支持 ?? 等语法，opencode 装不上）→ 装 Node 20 官方二进制（npmmirror 国内镜像）
NODE_OK=0
command -v node >/dev/null 2>&1 && [ "$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)" -ge 18 ] && NODE_OK=1
if [ "$NODE_OK" != "1" ]; then
    NODE_VER=v20.18.0
    ( cd /tmp && wget -q "https://npmmirror.com/mirrors/node/${NODE_VER}/node-${NODE_VER}-linux-x64.tar.xz" \
      && tar xf "node-${NODE_VER}-linux-x64.tar.xz" -C /usr/local --strip-components=1 )
    hash -r
fi
echo "  node $(node -v)  npm $(npm -v)"
npm config set registry https://registry.npmmirror.com
command -v opencode >/dev/null 2>&1 || npm install -g opencode-ai

echo "[2/6] 系统工具 + venv + Python 依赖 + 注册 opencode（复用 setup.sh）"
bash setup.sh
source .venv/bin/activate

echo "[3/6] 锁 transformers==4.49（codet5p .bin 加载所需）+ accelerate"
pip install -q "transformers==4.49.0" "accelerate>=0.27"

echo "[4/6] 下模型（hf-mirror，离线加载前置）"
python - <<'PY'
import os
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
from huggingface_hub import snapshot_download
for m in ["Salesforce/codet5p-110m-embedding", "bigcode/starcoder2-3b"]:
    print("  downloading", m)
    snapshot_download(m)
print("  models ready")
PY

echo "[5/6] GPU 自检"
python - <<'PY'
import torch
ok = torch.cuda.is_available()
print("  CUDA:", ok, torch.cuda.get_device_name(0) if ok else "(无 GPU!)")
PY

echo "[6/6] 完成。"
echo ""
echo "==== 还差两步（手动）===================================="
echo "  ① 传入向量库：把本机 data/db 放到  $(pwd)/data/db  （对比报告必需，约 2.8G）"
echo "  ② 填 API key：编辑 config.toml 的 key，然后  python setup_opencode.py"
echo ""
echo "  然后开跑（后台 + 日志）："
echo "    nohup python run_batch.py > data/output/_batch/run.log 2>&1 &"
echo "    tail -f data/output/_batch/run.log"
echo "========================================================"
