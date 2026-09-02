# 日报工作台（可共享版）

上午 / 下午分点写日报，周报可勾选日期、逐条挑选并改写成汇报口径，支持导出 Markdown / 图片 PNG。
含应用中心（日报工作台 + 博客系统），博客支持私有 / 公开，公开笔记在首页高大上画廊展示。

后端为 **FastAPI + SQLite**：账号密码用 scrypt 加盐哈希，token 用 HMAC 签名（无状态），数据按用户隔离，全部落盘到 SQLite，天然持久、便于备份。

## 本地运行

需要 Python 3.11+。

```bash
# 1) 创建并激活虚拟环境（首次）
python -m venv venv
venv/bin/pip install -r requirements.txt     # Windows: venv\Scripts\pip install -r requirements.txt

# 2) 启动（前后端一体，默认 http://localhost:8787）
venv/bin/uvicorn app:app --host 0.0.0.0 --port 8787
# 改端口： PORT=9000 venv/bin/uvicorn app:app --port 9000
# 改数据目录（SQLite 库文件位置）： DATA_DIR=/path/to/data venv/bin/uvicorn app:app
```

数据落在 `./data/app.db`（SQLite），`DATA_DIR` 可指向其他目录；首次启动若发现同目录下的旧 `*.json` 会自动迁移进 SQLite。

## 部署到云端（出公网网址，真·共享）

标准 Python 项目，可部署到任意支持 Python 的平台（Railway / Render / 你的 VPS）。
后端为服务端持久化，不再有「前端降级本地存储」分支——因此部署时请务必保证 `DATA_DIR` 指向持久卷，否则容器重启会丢数据。

### 方式 A：Railway / Render

1. 把本目录推到 GitHub 仓库（见下方「推到 GitHub」）。
2. 新建项目 → Deploy from GitHub repo → 选中该仓库。
3. Build 命令：`pip install -r requirements.txt`；Start 命令：`uvicorn app:app --host 0.0.0.0 --port $PORT`。
4. **持久化（重要）**：挂持久卷 Mount Path `/data`，并在环境变量设 `DATA_DIR=/data`。
5. 部署完成即获得公网网址，任何人打开即用、数据共享。

### 方式 B：自己的服务器 / VPS

```bash
git clone <你的仓库> && cd <目录>
python -m venv venv && venv/bin/pip install -r requirements.txt
# 用 systemd 守护（见 deploy/daily-report.service），或：
nohup venv/bin/uvicorn app:app --host 0.0.0.0 --port 8787 &
```
如需域名 + HTTPS，前面套一层 Nginx 反代（见 `deploy/nginx-daily-report.conf`）。数据落在服务器磁盘，天然持久。

## 推到 GitHub（本机执行）

```bash
cd <本项目目录>
git init
git add .
git commit -m "日报工作台 FastAPI + SQLite 版"
git branch -M main
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git push -u origin main
```

## 目录结构

- `app.py` —— FastAPI 后端 + SQLite 存储（端口 `PORT`，数据目录 `DATA_DIR`，静态托管 `public/`）
- `server.js` —— 旧版 Node 后端（已弃用，仅作兼容参考；SQLite 版无需它）
- `public/index.html` —— 应用本体（应用中心 / 日报 / 统计 / 周报 / 博客 + 图片导出），自包含单文件
- `data/app.db` —— SQLite 数据库（运行时自动生成，已加入 .gitignore）
- `requirements.txt` —— Python 依赖
- `deploy/` —— systemd 服务文件与 Nginx 反代配置

