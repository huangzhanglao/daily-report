# 日报工作台（可共享版）

上午 / 下午分点写日报，周报可勾选日期、逐条挑选并改写成汇报口径，支持导出 Markdown / 图片 PNG。
数据由服务端持久化（磁盘文件），任何人通过同一网址访问即看到**同一份共享数据**。

## 本地运行

```bash
node server.js
# 默认 http://localhost:8787
# 改端口： PORT=9000 node server.js
# 改数据目录： DATA_DIR=/path/to/data node server.js
```

数据落在 `./data/reports.json`（可用 `DATA_DIR` 指定到其他目录）。

## 部署到云端（出公网网址，真·共享）

本目录是一个标准的 Node 项目，可直接部署到任意支持 Node 的平台（Railway / Render / 你的 VPS）。
前端已内置「后端不可用自动降级到浏览器本地存储」，所以即使平台临时不可用，功能也不中断。

### 方式 A：Railway / Render（推荐，直接读 GitHub 仓库自动部署）

1. 把本目录推到你的 GitHub 仓库（见下方「推到 GitHub」）。
2. 在 Railway 或 Render 新建项目 → 选择「Deploy from GitHub repo」→ 选中该仓库。
3. 构建/启动命令保持默认（`npm start` 即 `node server.js`）。
4. **持久化（重要）**：平台容器重启会清空文件系统，需挂持久卷：
   - Railway：项目里 Add Volume，Mount Path 填 `/data`，并在 Variables 设 `DATA_DIR=/data`。
   - Render：创建 Disk，Mount Path 填 `/data`，并在 Environment 设 `DATA_DIR=/data`。
5. 部署完成后平台给出公网网址，任何人打开即用、数据共享。

### 方式 B：自己的服务器 / VPS

```bash
git clone <你的仓库> && cd <目录>
npm start   # 或 nohup node server.js &  /  pm2 start server.js
```
如需域名 + HTTPS，前面套一层 Nginx 反代即可。数据落在服务器磁盘，天然持久。

## 推到 GitHub（本机执行）

```bash
cd <本项目目录>
git init
git add .
git commit -m "日报工作台 可共享版"
git branch -M main
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git push -u origin main
```

## 目录结构

- `server.js` —— 零依赖 Node 服务（端口 `PORT`，数据目录 `DATA_DIR`）
- `public/index.html` —— 应用本体（日报 / 统计 / 周报 + 图片导出），自包含单文件
- `data/reports.json` —— 日报数据（运行时自动生成，已加入 .gitignore）
