#!/usr/bin/env bash
set -e
# 12306 代理 · 原生运行（无 Docker 时，Linux / macOS / WSL 可用）
# 用法： bash deploy/train_proxy/start.sh

cd "$(dirname "$0")/../.."   # 切回项目根目录，使 requirements.txt 与 train_proxy.py 可达

echo ">> 安装 Python 依赖 ..."
python3 -m pip install -r requirements.txt

echo ">> 安装 Playwright Chromium 浏览器（首次需要，约 150MB）..."
python3 -m playwright install chromium

# 公网部署建议设置 Token，避免被他人滥用刷 12306
# export PROXY_TOKEN=你的强Token

echo ">> 启动 12306 代理，监听 0.0.0.0:8799 ..."
python3 train_proxy.py
