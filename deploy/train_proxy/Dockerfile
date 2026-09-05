# 12306 实时查票代理镜像
# 基于官方 Playwright 镜像（已含 Python + Chromium + 系统依赖），无需再下载浏览器
FROM mcr.microsoft.com/playwright/python:v1.62.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 仅复制所需文件以利用层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY train_proxy.py ./

EXPOSE 8799

# PROXY_TOKEN 可选：公网部署务必设置，避免被他人滥用刷 12306
#   例： docker run -e PROXY_TOKEN=你的强Token -p 8799:8799 ...
# 调用方（daily-report）需在「数据源配置」页填写同一 TRAIN_API_TOKEN
CMD ["python", "train_proxy.py"]
