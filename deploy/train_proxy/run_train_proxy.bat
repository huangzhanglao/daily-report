@echo off
REM 12306 代理 · Windows 原生运行（无 Docker 时）
REM 用法： 双击本文件，或在项目根目录执行  deploy\train_proxy\run_train_proxy.bat

cd /d "%~dp0\..\.."

echo Installing Python dependencies ...
python -m pip install -r requirements.txt

echo Installing Playwright Chromium browser (first time only, ~150MB) ...
python -m playwright install chromium

REM 公网部署建议设置 Token：
REM set PROXY_TOKEN=your_strong_token

echo Starting 12306 proxy on 0.0.0.0:8799 ...
python train_proxy.py
