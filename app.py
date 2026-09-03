# -*- coding: utf-8 -*-
"""日报工作台 —— FastAPI 装配层（瘦主入口）。

历史：原为单文件 app.py（1846 行）。已将共享基础设施抽到 core.py、各应用路由拆到 routes/*.py，
本文件只负责「装配」：
  1) 触发 core 的建表 / 迁移 / 种子
  2) 创建 FastAPI 实例并挂全局安全中间件
  3) 逐个 include 各应用 router
  4) 静态资源挂载

新增一个应用时：在 routes/ 下新建一个 xxx.py（定义 router），并在此 include 即可，无需改动其它文件。
启动： uvicorn app:app --host 0.0.0.0 --port 8787
云部署： PORT / DATA_DIR 由环境变量注入；SQLite 库文件位于 DATA_DIR/app.db
"""

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import core
from core import init_db, migrate_from_json, seed_app_catalog, seed_mortgage_regions, PUBLIC_DIR

# ---------- 建表 / 迁移 / 种子（与拆分前相同的启动触发顺序） ----------
init_db()
migrate_from_json()
seed_app_catalog()
seed_mortgage_regions()

app = FastAPI(title="日报工作台 API")


# ---------- 安全响应头（缓解点击劫持 / XSS 外联注入 / MIME 嗅探） ----------
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")  # 禁止被 iframe 嵌入，防点击劫持
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-XSS-Protection", "1; mode=block")
    # 阻断从第三方域加载脚本/样式（缓解外联 XSS 注入）；本站大量使用内联脚本，故保留 'unsafe-inline'
    csp = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; "
        "font-src 'self' data:; "
        "connect-src 'self'"
    )
    response.headers.setdefault("Content-Security-Policy", csp)
    # 仅当确实走 HTTPS（含反向代理转发的 x-forwarded-proto）才下发 HSTS，避免纯 HTTP 本地部署被误锁
    if request.headers.get("x-forwarded-proto", "").lower() == "https" or request.url.scheme == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


# ---------- 各应用路由 ----------
from routes import auth, reports, finance, tax, mortgage, ledger, llm, admin

app.include_router(auth.router)
app.include_router(reports.router)
app.include_router(finance.router)
app.include_router(tax.router)
app.include_router(mortgage.router)
app.include_router(ledger.router)
app.include_router(llm.router)
app.include_router(admin.router)


# ---------- 健康检查 + 首页 ----------
@app.get("/api/health")
def health():
    import time
    return {"ok": True, "time": int(time.time() * 1000)}


@app.get("/")
def index():
    return FileResponse(str(PUBLIC_DIR / "index.html"))


# 静态资源（前端单页，public/ 目录），须放在最后，否则会吞掉上面的 API 路由
app.mount("/", StaticFiles(directory=str(PUBLIC_DIR), html=True), name="static")
