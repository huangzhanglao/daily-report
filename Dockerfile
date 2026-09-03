# syntax=docker/dockerfile:1
# ============================================================
# 日报工作台 —— FastAPI 应用镜像
# 构建上下文 = 项目根目录（含 app.py / core.py / routes/ / public/ / requirements.txt）
# 设计要点：
#   - 多阶段：build 装依赖 -> runtime 只拷贝已安装的 site-packages，镜像更小
#   - 非 root 运行（uid=10001），贴合项目安全加固理念
#   - 数据目录 /app/data 用命名卷挂载；DATA_DIR 由 compose 通过环境变量指向它
#   - SECRET(.secret) 存于 data 目录内，随卷持久化，重启不丢 token 签名密钥
# ============================================================

# ---------- 阶段 1：依赖构建 ----------
FROM python:3.13-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# 先拷贝依赖清单，最大化利用构建缓存
COPY requirements.txt .
# 用 --prefix 把依赖装到 /deps（勿与 --user 混用，二者互斥）。
# /deps 下结构为 lib/python3.x/site-packages，拷到 /usr/local 即进入默认 site-packages。
RUN pip install --prefix /deps -r requirements.txt

# ---------- 阶段 2：运行镜像 ----------
FROM python:3.13-slim

# 安装 tzdata 以支持 TZ 环境变量正确解析时区（slim 镜像默认无时区库）
RUN apt-get update && \
    apt-get install -y --no-install-recommends tzdata && \
    rm -rf /var/lib/apt/lists/*

# 数据目录默认 /app/data（compose 用命名卷挂载到该路径，.secret 随卷持久化）
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8787 \
    DATA_DIR=/app/data

WORKDIR /app

# 拷贝依赖
COPY --from=builder /deps /usr/local

# 拷贝应用源码与静态资源
COPY app.py core.py requirements.txt ./
COPY routes/ ./routes/
COPY public/ ./public/

# 建数据目录并交给非 root 用户
RUN mkdir -p /app/data && \
    addgroup --system --gid 10001 app && \
    adduser --system --uid 10001 --ingroup app --home /app app && \
    chown -R app:app /app

# 健康检查：探活 /api/health（PORT 从镜像/容器环境变量读取，默认 8787）
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8787')+'/api/health',timeout=3)"

USER app

# 默认启动命令（compose 里通常也会显式覆盖以传 --workers 等）
EXPOSE 8787
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8787"]
