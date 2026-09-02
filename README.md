# 应用中心（可共享工作台）

一个统一的**应用中心**（工作台），内置多个相互隔离的子应用，并配有完整的**设置 / 管理后台**：

- **日报工作台** —— 上午 / 下午分点写日报，周报可勾选、改写、导出 Markdown / 图片长图。
- **博客系统** —— 对标 CSDN 写笔记、发文章，支持私有 / 公开，公开内容在首页广场展示。
- **财务工作台** —— 一整年 12 个月财务日历：报税 / 结账 / 报表排期、农历节假日、提醒与多端导出。
- **报税工作台** —— 多公司申报事项一屏掌握：征期自动算截止日、完成率进度环、按公司 / 按税种总览、顺延申报。
- **买房计算器** —— 结合 2026 最新房贷政策：按地区预置利率 / 首付 / 公积金上限，算贷款总额、月供（等额本息 / 等额本金）、总利息，并估算契税 / 增值税 / 个税 / 中介费等购房总成本。所有参数可调，方案可保存。

所有数据均**按登录账号隔离**，落盘到 SQLite，天然持久、便于备份与迁移。

## 设置 / 管理后台（管理员）

应用中心右上角「⚙ 设置」进入管理后台（仅管理员可见），包含四个模块，全部数据落库：

1. **应用管理** —— 维护可上架的应用：名称、图标、入口（内置视图 `report` / `blog`，或独立页面如 `finance.html`）、简介、配色、是否启用、排序。
2. **权限管理** —— 以矩阵形式为**每位用户勾选可使用的应用**，实现「每个用户拥有独立的应用集合」。应用中心只展示被授权的应用。
3. **用户管理** —— 创建 / 编辑 / 删除账号，分配「管理员 / 普通用户」角色，重置密码。删除用户会级联清理其全部数据。
4. **操作说明 / 首页简介** —— 可编辑的「应用中心操作说明」与「首页顶部简介」，保存后立即对全体访客生效。

> 管理员身份：用户表的 `role='admin'`。初始化时若不存在管理员，会默认将 `username='admin'` 的用户（或最早注册用户）提升为管理员。

## 数据隔离模型

- **账号级隔离**：日报 / 博客 / 财务 / 报税 / 买房计算器 各自按 `user_id` 存储，A、B 用户数据互不可见。买房计算器另含地区政策预设表 `mortgage_regions`（含 2026 年各城市利率、首付、公积金上限，管理员可在 `/api/admin/mortgage-regions` 调整）与方案表 `mortgage_scenarios`。
- **应用级隔离**：`user_app_perm` 记录「用户 ↔ 应用」授权关系。未授权的应用不会出现在该用户的应用中心；匿名访客仅看到全部已启用应用的预览。
- 所有写操作均需登录；管理类接口额外要求 `role='admin'`（服务端校验，非仅前端隐藏）。

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

1. 把本目录推到 GitHub 仓库。
2. 新建项目 → Deploy from GitHub repo → 选中该仓库。
3. Build 命令：`pip install -r requirements.txt`；Start 命令：`uvicorn app:app --host 0.0.0.0 --port $PORT`。
4. **持久化（重要）**：挂持久卷 Mount Path `/data`，并在环境变量设 `DATA_DIR=/data`。
5. 部署完成即获得公网网址，任何人打开即用、数据共享。

### 方式 B：自己的服务器 / VPS

```bash
git clone <你的仓库> && cd <目录>
python -m venv venv && venv/bin/pip install -r requirements.txt
# 用 systemd 守护，或：
nohup venv/bin/uvicorn app:app --host 0.0.0.0 --port 8787 &
```

如需域名 + HTTPS，前面套一层 Nginx 反代。数据落在服务器磁盘，天然持久。

## 推到 GitHub（本机执行）

```bash
cd <本项目目录>
git add .
git commit -m "应用中心 FastAPI + SQLite 版"
git branch -M main
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git push -u origin main
```

## 目录结构

- `app.py` —— FastAPI 后端 + SQLite 存储（端口 `PORT`，数据目录 `DATA_DIR`，静态托管 `public/`）
- `public/index.html` —— 应用中心本体（应用中心 / 日报 / 统计 / 周报 / 博客）
- `public/settings.html` —— 管理后台（应用 / 权限 / 用户 / 操作说明）
- `public/finance.html` / `public/tax.html` —— 财务 / 报税子应用
- `data/app.db` —— SQLite 数据库（运行时自动生成，已加入 .gitignore）
- `requirements.txt` —— Python 依赖
- `deploy/` —— systemd 服务文件与 Nginx 反代配置

## 主要 API

- 认证：`POST /api/auth/register`、`POST /api/auth/login`、`GET /api/auth/me`
- 应用中心：`GET /api/apps`（按用户权限返回可见应用，匿名返回全部启用应用预览）
- 管理（需 `role=admin`）：`/api/admin/apps`（GET/POST/PUT/DELETE）、`/api/admin/users`（GET/POST/PUT/DELETE）、`/api/admin/permissions`（GET/PUT）
- 设置：`GET /api/settings`、`PUT /api/admin/settings`
- 业务数据（按 `user_id` 隔离）：日报 `/api/reports`、博客 `/api/blogs`、财务 `/api/finance/items`、报税 `/api/tax/items`
