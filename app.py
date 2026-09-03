# 日报工作台 —— FastAPI + SQLite 后端
# 替代原 Node server.js：接口契约、scrypt 密码哈希、HMAC token 完全对齐，前端无需改动。
# 启动： uvicorn app:app --host 0.0.0.0 --port 8787
# 云部署： PORT / DATA_DIR 由环境变量注入；SQLite 库文件位于 DATA_DIR/app.db

import os
import re
import json
import time
import base64
import secrets
import hashlib
import hmac
import sqlite3
import threading
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, Header, Depends
from fastapi.responses import JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
PUBLIC_DIR = ROOT / "public"
DATA_DIR = Path(os.environ.get("DATA_DIR", str(ROOT / "data"))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE = DATA_DIR / "app.db"
SECRET_FILE = DATA_DIR / ".secret"
PORT = int(os.environ.get("PORT", "8787"))

# 复用与 Node 版相同的 .secret（保证 token 连续性）
if not SECRET_FILE.exists():
    SECRET_FILE.write_text(secrets.token_hex(32), encoding="utf-8")
SECRET = SECRET_FILE.read_text(encoding="utf-8").strip() or "dev-secret"

write_lock = threading.Lock()


def get_conn():
    conn = sqlite3.connect(str(DB_FILE), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_conn()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                salt TEXT NOT NULL,
                hash TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reports (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                date TEXT NOT NULL,
                slot TEXT NOT NULL DEFAULT 'full',
                points TEXT NOT NULL DEFAULT '[]',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS blogs (
                id TEXT PRIMARY KEY,
                author_id TEXT NOT NULL,
                author_name TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '[]',
                visibility TEXT NOT NULL DEFAULT 'public',
                cover TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS finance_items (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                date_key TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                cat TEXT NOT NULL DEFAULT '',
                pri TEXT NOT NULL DEFAULT '中',
                done INTEGER NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT '',
                owner TEXT NOT NULL DEFAULT '',
                time TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS finance_meta (
                user_id TEXT PRIMARY KEY,
                initialized INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS tax_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                company TEXT NOT NULL DEFAULT '',
                tax_item TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '未处理',
                deadline TEXT NOT NULL DEFAULT '',
                month TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS app_catalog (
                id TEXT PRIMARY KEY,
                key TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                icon TEXT NOT NULL DEFAULT '',
                desc TEXT NOT NULL DEFAULT '',
                entry TEXT NOT NULL DEFAULT '',
                color TEXT NOT NULL DEFAULT '',
                sort INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                intro TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_app_perm (
                user_id TEXT NOT NULL,
                app_key TEXT NOT NULL,
                granted INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (user_id, app_key)
            );
            CREATE TABLE IF NOT EXISTS settings_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS mortgage_regions (
                id TEXT PRIMARY KEY,
                key TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                lpr5 TEXT NOT NULL DEFAULT '3.50',
                commercial_first TEXT NOT NULL DEFAULT '3.05',
                commercial_second TEXT NOT NULL DEFAULT '3.35',
                fund_first TEXT NOT NULL DEFAULT '2.60',
                fund_second TEXT NOT NULL DEFAULT '3.075',
                down_first TEXT NOT NULL DEFAULT '20',
                down_second TEXT NOT NULL DEFAULT '30',
                fund_cap_single TEXT NOT NULL DEFAULT '60',
                fund_cap_double TEXT NOT NULL DEFAULT '120',
                enabled INTEGER NOT NULL DEFAULT 1,
                sort INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS mortgage_scenarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                params TEXT NOT NULL DEFAULT '{}',
                result TEXT NOT NULL DEFAULT '{}',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            """
        )
        # 兼容旧库：补充 users 表可能缺失的列
        for col, ddl in [
            ("role", "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'"),
            ("display_name", "ALTER TABLE users ADD COLUMN display_name TEXT NOT NULL DEFAULT ''"),
        ]:
            try:
                conn.execute(ddl)
            except Exception:
                pass
        conn.commit()
    finally:
        conn.close()


def migrate_from_json():
    """一次性迁移：若 SQLite 为空且旧的 JSON 文件存在，则导入，保证数据不丢。"""
    conn = get_conn()
    try:
        cur = conn.execute("SELECT COUNT(*) AS c FROM users")
        if cur.fetchone()["c"] > 0:
            return
    finally:
        conn.close()

    users_file = DATA_DIR / "users.json"
    reports_file = DATA_DIR / "reports.json"
    blogs_file = DATA_DIR / "blogs.json"
    if not (users_file.exists() or reports_file.exists() or blogs_file.exists()):
        return

    print("[migrate] 从旧 JSON 文件导入数据到 SQLite …")

    def load_json(p):
        if not p.exists():
            return []
        try:
            return json.loads(p.read_text(encoding="utf-8")) or []
        except Exception:
            return []

    users = load_json(users_file)
    reports = load_json(reports_file)
    blogs = load_json(blogs_file)

    with write_lock:
        conn = get_conn()
        try:
            for u in users:
                conn.execute(
                    "INSERT OR IGNORE INTO users (id, username, salt, hash, created_at) VALUES (?,?,?,?,?)",
                    (u.get("id"), u.get("username"), u.get("salt"), u.get("hash"), int(u.get("createdAt", time.time() * 1000))),
                )
            for r in reports:
                conn.execute(
                    "INSERT OR IGNORE INTO reports (id, user_id, date, slot, points, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        r.get("id"),
                        r.get("userId"),
                        r.get("date", ""),
                        r.get("slot", "full"),
                        json.dumps(r.get("points", []), ensure_ascii=False),
                        int(r.get("createdAt", time.time() * 1000)),
                        int(r.get("updatedAt", time.time() * 1000)),
                    ),
                )
            for b in blogs:
                conn.execute(
                    "INSERT OR IGNORE INTO blogs (id, author_id, author_name, title, summary, content, category, tags, visibility, cover, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        b.get("id"),
                        b.get("authorId"),
                        b.get("authorName", ""),
                        b.get("title", ""),
                        b.get("summary", ""),
                        b.get("content", ""),
                        b.get("category", ""),
                        json.dumps(b.get("tags", []), ensure_ascii=False),
                        b.get("visibility", "public"),
                        b.get("cover", ""),
                        int(b.get("createdAt", time.time() * 1000)),
                        int(b.get("updatedAt", time.time() * 1000)),
                    ),
                )
            conn.commit()
        finally:
            conn.close()
    print(f"[migrate] 完成：{len(users)} 用户 / {len(reports)} 日报 / {len(blogs)} 笔记")


# ---------------- 应用目录 / 权限 / 设置 种子与辅助 ----------------
DEFAULT_APPS = [
    {"key": "report", "name": "日报工作台", "icon": "📋",
     "desc": "记录每日上午 / 下午的工作，按点汇总，一键生成可汇报给领导的周报长图。",
     "entry": "report", "color": "linear-gradient(135deg,#3b6cff,#7aa0ff)", "sort": 1},
    {"key": "blog", "name": "博客系统", "icon": "📚",
     "desc": "对标 CSDN 写笔记、发文章。支持私有 / 公开，公开内容可在首页广场展示。",
     "entry": "blog", "color": "linear-gradient(135deg,#11b886,#37d6a4)", "sort": 2},
    {"key": "finance", "name": "财务工作台", "icon": "💰",
     "desc": "一整年 · 12 个月财务日历：报税 / 结账 / 报表排期、农历节假日、提醒与多端导出。数据按账号隔离存档。",
     "entry": "finance.html", "color": "linear-gradient(135deg,#f59e0b,#fbbf24)", "sort": 3},
    {"key": "tax", "name": "报税工作台", "icon": "🧾",
     "desc": "多公司申报事项一屏掌握：按征期日历自动算截止日、完成率进度环、按公司 / 按税种总览、顺延申报。数据按账号隔离存档。",
     "entry": "tax.html", "color": "linear-gradient(135deg,#6366f1,#8b5cf6)", "sort": 4},
    {"key": "mortgage", "name": "买房计算器", "icon": "🏠",
     "desc": "结合最新房贷政策：按地区选预设利率 / 首付 / 公积金上限，算贷款总额、月供、总利息，并估算契税 / 增值税 / 个税 / 中介费等购房总成本。所有参数可调。",
     "entry": "mortgage.html", "color": "linear-gradient(135deg,#ef4444,#f97316)", "sort": 5},
]

DEFAULT_SETTINGS = {
    "allow_register": "0",  # 默认关闭公开注册（防垃圾注册）；系统尚无用户时仍允许注册首位管理员
    "home_intro": "选择你要进入的应用。所有数据按账号隔离存储，安全可追溯。",
    "app_center_title": "🧭 应用中心",
    "guide": (
        "【应用中心 · 操作说明】\n\n"
        "1. 应用中心是统一入口，所有应用的数据均按登录账号隔离，互不干扰。\n"
        "2. 管理员可在「设置」中管理：\n"
        "   · 应用管理：维护可上架的应用（名称、图标、入口、简介、配色、是否启用、排序）。\n"
        "   · 权限管理：为每位用户勾选可使用的应用，实现「每个用户拥有独立的应用集合」。\n"
        "   · 用户管理：创建 / 编辑 / 停用账号，分配管理员角色。\n"
        "   · 操作说明：本说明与首页简介均可编辑。\n"
        "3. 普通用户登录后只看到被授权的应用；未授权应用不会出现在他的应用中心。\n"
        "4. 首页简介展示在应用中心顶部，管理员可在「设置 → 操作说明」中修改。"
    ),
}

# 买房计算器：地区预设政策（2026年最新，数据可后台调整，前端亦可手动覆盖）
DEFAULT_REGIONS = [
    # key, name, lpr5, 商贷首套, 商贷二套, 公积金首套, 公积金二套, 首付首套%, 首付二套%, 公积金上限单缴存(万), 双缴存(万), sort
    ("beijing", "北京（一线）", "3.50", "3.05", "3.35", "2.60", "3.075", "20", "35", "120", "120", 1),
    ("shanghai", "上海（一线）", "3.50", "3.05", "3.35", "2.60", "3.075", "20", "30", "65", "130", 2),
    ("guangzhou", "广州（一线）", "3.50", "3.05", "3.35", "2.60", "3.075", "20", "30", "60", "100", 3),
    ("shenzhen", "深圳（一线）", "3.50", "3.05", "3.05", "2.60", "3.075", "20", "30", "60", "110", 4),
    ("hangzhou", "杭州（新一线）", "3.50", "3.05", "3.35", "2.60", "3.075", "20", "30", "65", "130", 5),
    ("chengdu", "成都（新一线）", "3.50", "3.05", "3.35", "2.60", "3.075", "20", "30", "60", "90", 6),
    ("wuhan", "武汉（新一线）", "3.50", "3.05", "3.35", "2.60", "3.075", "20", "30", "60", "80", 7),
    ("xian", "西安（新一线）", "3.50", "3.05", "3.35", "2.60", "3.075", "20", "30", "65", "85", 8),
    ("suzhou", "苏州（新一线）", "3.50", "3.05", "3.35", "2.60", "3.075", "20", "30", "80", "150", 9),
    ("national", "全国通用（参考）", "3.50", "3.05", "3.35", "2.60", "3.075", "20", "30", "60", "100", 99),
]


def seed_mortgage_regions():
    conn = get_conn()
    try:
        cnt = conn.execute("SELECT COUNT(*) AS c FROM mortgage_regions").fetchone()["c"]
        if cnt > 0:
            return
        now = int(time.time() * 1000)
        for r in DEFAULT_REGIONS:
            conn.execute(
                "INSERT INTO mortgage_regions (id,key,name,lpr5,commercial_first,commercial_second,fund_first,fund_second,down_first,down_second,fund_cap_single,fund_cap_double,enabled,sort) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?)",
                (uid_now(), r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10], r[11]),
            )
        conn.commit()
    finally:
        conn.close()


def grant_all_apps(uid: str):
    """给某个用户授予当前所有已启用的应用。"""
    conn = get_conn()
    try:
        apps = conn.execute("SELECT key FROM app_catalog WHERE enabled=1").fetchall()
        for a in apps:
            conn.execute(
                "INSERT OR IGNORE INTO user_app_perm (user_id, app_key, granted) VALUES (?,?,1)",
                (uid, a["key"]),
            )
        conn.commit()
    finally:
        conn.close()


def grant_app_to_all_users(app_key: str):
    """把某个应用授予所有用户。"""
    conn = get_conn()
    try:
        users = conn.execute("SELECT id FROM users").fetchall()
        for u in users:
            conn.execute(
                "INSERT OR IGNORE INTO user_app_perm (user_id, app_key, granted) VALUES (?,?,1)",
                (u["id"], app_key),
            )
        conn.commit()
    finally:
        conn.close()


def seed_app_catalog():
    conn = get_conn()
    try:
        now = int(time.time() * 1000)
        # 逐个 upsert 默认应用（即使目录已存在，也能补上新上架的应用，如买房计算器）
        for app in DEFAULT_APPS:
            exists = conn.execute("SELECT 1 FROM app_catalog WHERE key=?", (app["key"],)).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO app_catalog (id,key,name,icon,desc,entry,color,sort,enabled,intro,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,1,'',?,?)",
                    (uid_now(), app["key"], app["name"], app["icon"], app["desc"],
                     app["entry"], app["color"], app["sort"], now, now),
                )
        conn.commit()
        # 保证每位用户对每个已启用应用都有一条授权记录（默认授予，管理员可收回）
        apps = conn.execute("SELECT key FROM app_catalog WHERE enabled=1").fetchall()
        users = conn.execute("SELECT id FROM users").fetchall()
        for u in users:
            for a in apps:
                conn.execute(
                    "INSERT OR IGNORE INTO user_app_perm (user_id, app_key, granted) VALUES (?,?,1)",
                    (u["id"], a["key"]),
                )
        # 至少保证存在一个管理员：优先 username='admin'，否则最早注册的用户
        admin_exists = conn.execute("SELECT COUNT(*) AS c FROM users WHERE role='admin'").fetchone()["c"]
        if admin_exists == 0:
            adm = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()
            if not adm:
                adm = conn.execute("SELECT id FROM users ORDER BY created_at ASC LIMIT 1").fetchone()
            if adm:
                conn.execute("UPDATE users SET role='admin' WHERE id=?", (adm["id"],))
        # 设置项默认值
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings_meta (key, value) VALUES (?,?)", (k, v))
        conn.commit()
    finally:
        conn.close()


init_db()
migrate_from_json()


# ---------------- 密码哈希：与 Node crypto.scryptSync(pw, salt, 64) 对齐 ----------------
# Node: salt = randomBytes(16).toString('hex') -> 32 字符；scryptSync(pw, salt, 64) 把 salt 字符串按 utf8 编码为字节。
def _scrypt_hex(pw: str, salt_hex: str) -> str:
    salt_bytes = salt_hex.encode("utf-8")
    dk = hashlib.scrypt(pw.encode("utf-8"), salt=salt_bytes, n=16384, r=8, p=1, dklen=64)
    return dk.hex()


def hash_password(pw: str):
    salt = secrets.token_hex(16)
    return {"salt": salt, "hash": _scrypt_hex(pw, salt)}


def verify_password(pw: str, salt_hex: str, hash_hex: str) -> bool:
    try:
        return hmac.compare_digest(_scrypt_hex(pw, salt_hex), hash_hex)
    except Exception:
        return False


# ---------------- token：与 Node signToken/verifyToken 对齐 ----------------
# payload = base64url(JSON({uid, exp})) + "." + base64url(HMAC-SHA256(secret, payload))
def _b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)


def sign_token(uid: str) -> str:
    payload = _b64url_encode(json.dumps({"uid": uid, "exp": int(time.time() * 1000) + 1000 * 60 * 60 * 24 * 30}).encode("utf-8"))
    sig = _b64url_encode(hmac.new(SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest())
    return payload + "." + sig


def verify_token(token: str):
    if not token or not isinstance(token, str) or "." not in token:
        return None
    parts = token.split(".")
    if len(parts) != 2:
        return None
    expect = _b64url_encode(hmac.new(SECRET.encode("utf-8"), parts[0].encode("utf-8"), hashlib.sha256).digest())
    if not hmac.compare_digest(parts[1], expect):
        return None
    try:
        data = json.loads(_b64url_decode(parts[0]))
        if not data.get("exp") or data["exp"] < int(time.time() * 1000):
            return None
        return data.get("uid")
    except Exception:
        return None


# ---------- 速率限制 + 注册开关 ----------
# 限流后端：默认内存（单进程部署足够）；设置环境变量 REDIS_URL 后切换为 Redis 共享存储，
# 适用于多 worker / 多实例部署，避免各进程各自计数放大放行额度。Redis 不可达时自动回退内存。
_rate_hits = {}                 # 内存兜底计数
_rate_redis = None              # Redis 客户端（惰性初始化）
_rate_redis_tried = False

def _init_redis():
    """惰性连接 Redis：仅在配置了 REDIS_URL 且可达时使用；否则返回 None 走内存兜底。"""
    global _rate_redis, _rate_redis_tried
    if _rate_redis_tried:
        return _rate_redis
    _rate_redis_tried = True
    url = os.environ.get("REDIS_URL")
    if not url:
        return None
    try:
        import redis  # 延迟导入：未安装也不影响应用启动（走内存兜底）
        client = redis.Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)
        client.ping()
        _rate_redis = client
    except Exception as e:
        import sys
        print("[rate-limit] Redis 不可用，回退内存限流：%s" % e, file=sys.stderr)
        _rate_redis = None
    return _rate_redis

def rate_limit(key: str, max_count: int, window_seconds: int) -> bool:
    r = _init_redis()
    if r is not None:
        # Redis 滑动窗口：有序集合按时间戳计分，剔除窗口外记录后统计 + 设过期
        now = time.time()
        member = "%.6f:%s" % (now, secrets.token_hex(4))
        try:
            pipe = r.pipeline()
            pipe.zremrangebyscore(key, 0, now - window_seconds)
            pipe.zadd(key, {member: now})
            pipe.zcard(key)
            pipe.expire(key, window_seconds)
            count = pipe.execute()[2]
        except Exception:
            return _rate_limit_mem(key, max_count, window_seconds)
        return count <= max_count
    return _rate_limit_mem(key, max_count, window_seconds)

def _rate_limit_mem(key: str, max_count: int, window_seconds: int) -> bool:
    now = time.time()
    hits = _rate_hits.setdefault(key, [])
    hits[:] = [t for t in hits if now - t < window_seconds]
    if len(hits) >= max_count:
        return False
    hits.append(now)
    return True

def client_ip(request: Request) -> str:
    # 尊重反向代理转发的真实客户端 IP
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

def public_register_enabled() -> bool:
    # 系统尚无任何用户时（首次初始化），允许注册以创建首位管理员；
    # 一旦存在用户，则依据设置项 allow_register 决定（默认关闭，防垃圾注册）。
    conn = get_conn()
    try:
        cnt = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        if cnt == 0:
            return True
        row = conn.execute("SELECT value FROM settings_meta WHERE key='allow_register'").fetchone()
    finally:
        conn.close()
    return bool(row and str(row["value"]).strip().lower() in ("1", "true", "on", "yes"))

AUTH_COOKIE = "drb_token"

def _is_https(request: Request) -> bool:
    return request.headers.get("x-forwarded-proto", "").lower() == "https" or request.url.scheme == "https"

def set_auth_cookie(response, token: str, request: Request):
    # HttpOnly：JS 不可读，根治 XSS 窃 token；SameSite=Lax：缓解 CSRF；HTTPS 下追加 Secure
    response.set_cookie(AUTH_COOKIE, token, httponly=True, samesite="lax",
                        secure=_is_https(request), path="/", max_age=60 * 60 * 24 * 30)

def clear_auth_cookie(response):
    response.delete_cookie(AUTH_COOKIE, path="/")

def get_uid(authorization: str = Header(default=""), request: Request = None) -> str:
    token = None
    if request is not None:
        token = request.cookies.get(AUTH_COOKIE)  # 优先走 HttpOnly cookie（防 XSS 读取）
    if not token and authorization:
        m = re.match(r"^Bearer\s+(.+)$", authorization or "", re.I)
        if m:
            token = m.group(1)
    if not token:
        return None
    return verify_token(token)


def require_uid(authorization: str = Header(default=""), request: Request = None) -> str:
    uid = get_uid(authorization, request)
    if not uid:
        raise HTTPException(401, "未登录")
    return uid


def require_admin(authorization: str = Header(default=""), request: Request = None) -> str:
    uid = get_uid(authorization, request)
    if not uid:
        raise HTTPException(401, "未登录")
    conn = get_conn()
    try:
        u = conn.execute("SELECT role FROM users WHERE id=?", (uid,)).fetchone()
    finally:
        conn.close()
    if not u or u["role"] != "admin":
        raise HTTPException(403, "需要管理员权限")
    return uid


def uid_now() -> str:
    return base64.urlsafe_b64encode(os.urandom(9)).rstrip(b"=").decode("ascii") + secrets.token_hex(3)


# 应用目录 / 权限 / 设置 种子（依赖 uid_now，故放在其定义之后）
seed_app_catalog()
seed_mortgage_regions()



def row_to_user(r) -> dict:
    return {
        "id": r["id"],
        "username": r["username"],
        "role": r["role"] if "role" in r.keys() else "user",
        "displayName": r["display_name"] if "display_name" in r.keys() else "",
    }


def blog_public(b) -> dict:
    return {
        "id": b["id"],
        "title": b["title"],
        "summary": b["summary"] or "",
        "tags": json.loads(b["tags"] or "[]"),
        "authorId": b["author_id"],
        "authorName": b["author_name"],
        "visibility": b["visibility"] or "public",
        "cover": b["cover"] or "",
        "category": b["category"] or "",
        "createdAt": b["created_at"],
        "updatedAt": b["updated_at"],
    }


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


@app.get("/api/health")
def health():
    return {"ok": True, "time": int(time.time() * 1000)}


# ---------------- 认证 ----------------
@app.post("/api/auth/register")
async def register(body: dict, response: Response, request: Request):
    ip = client_ip(request)
    if not rate_limit("reg:" + ip, 5, 600):
        raise HTTPException(429, "注册过于频繁，请稍后再试")
    if not public_register_enabled():
        raise HTTPException(403, "公开注册已关闭：请联系管理员在「设置 → 操作说明」中开启，或由管理员为你创建账号")
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not (3 <= len(username) <= 20):
        raise HTTPException(400, "用户名需 3-20 位")
    if len(password) < 6:
        raise HTTPException(400, "密码至少 6 位")
    conn = get_conn()
    try:
        exists = conn.execute("SELECT id FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone()
        if exists:
            raise HTTPException(409, "该用户名已被注册")
        hp = hash_password(password)
        u_id = uid_now()
        now = int(time.time() * 1000)
        conn.execute("INSERT INTO users (id, username, salt, hash, created_at) VALUES (?,?,?,?,?)",
                     (u_id, username, hp["salt"], hp["hash"], now))
        conn.commit()
    finally:
        conn.close()
    # token 仅写入 HttpOnly cookie，前端 JS 不可读取，根治 XSS 窃 token
    set_auth_cookie(response, sign_token(u_id), request)
    return {"user": {"id": u_id, "username": username}}


@app.post("/api/auth/login")
async def login(body: dict, response: Response, request: Request):
    ip = client_ip(request)
    if not rate_limit("login:" + ip, 12, 300):
        raise HTTPException(429, "登录尝试过于频繁，请稍后再试")
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    conn = get_conn()
    try:
        u = conn.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone()
    finally:
        conn.close()
    if not u or not verify_password(password, u["salt"], u["hash"]):
        raise HTTPException(401, "用户名或密码错误")
    # token 仅写入 HttpOnly cookie，前端 JS 不可读取，根治 XSS 窃 token
    set_auth_cookie(response, sign_token(u["id"]), request)
    return {"user": row_to_user(u)}


@app.post("/api/auth/logout")
async def logout(response: Response):
    clear_auth_cookie(response)
    return {"ok": True}


@app.get("/api/auth/me")
def me(uid: str = Depends(require_uid)):
    conn = get_conn()
    try:
        u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    finally:
        conn.close()
    if not u:
        raise HTTPException(401, "用户不存在")
    return {"user": row_to_user(u)}


# ---------------- 日报（强制登录 + 按用户隔离） ----------------
@app.get("/api/reports")
def list_reports(uid: str = Depends(require_uid)):
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM reports WHERE user_id=?", (uid,)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        out.append({
            "id": r["id"], "userId": r["user_id"], "date": r["date"], "slot": r["slot"],
            "points": json.loads(r["points"] or "[]"), "createdAt": r["created_at"], "updatedAt": r["updated_at"],
        })
    return out


@app.post("/api/reports")
async def create_report(body: dict, uid: str = Depends(require_uid)):
    now = int(time.time() * 1000)
    r_id = body.get("id") or uid_now()
    points = body.get("points")
    if not isinstance(points, list):
        points = []
    with write_lock:
        conn = get_conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO reports (id, user_id, date, slot, points, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (r_id, uid, (body.get("date") or "").strip(), body.get("slot", "full"),
                 json.dumps(points, ensure_ascii=False), int(body.get("createdAt", now)), now),
            )
            conn.commit()
        finally:
            conn.close()
    return {"id": r_id, "userId": uid, "date": body.get("date", ""), "slot": body.get("slot", "full"),
            "points": points, "createdAt": int(body.get("createdAt", now)), "updatedAt": now}


@app.put("/api/reports/{rid}")
async def update_report(rid: str, body: dict, uid: str = Depends(require_uid)):
    with write_lock:
        conn = get_conn()
        try:
            r = conn.execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
            if not r:
                raise HTTPException(404, "not found")
            if r["user_id"] != uid:
                raise HTTPException(403, "无权修改")
            points = body.get("points")
            if not isinstance(points, list):
                points = json.loads(r["points"] or "[]")
            conn.execute(
                "UPDATE reports SET date=?, slot=?, points=?, updated_at=? WHERE id=?",
                ((body.get("date") or r["date"]).strip(), body.get("slot", r["slot"]),
                 json.dumps(points, ensure_ascii=False), int(time.time() * 1000), rid),
            )
            conn.commit()
        finally:
            conn.close()
    return {"id": rid, "userId": uid, "date": body.get("date", r["date"]), "slot": body.get("slot", r["slot"]),
            "points": points, "createdAt": r["created_at"], "updatedAt": int(time.time() * 1000)}


@app.delete("/api/reports/{rid}")
def delete_report(rid: str, uid: str = Depends(require_uid)):
    conn = get_conn()
    try:
        r = conn.execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
        if not r:
            raise HTTPException(404, "not found")
        if r["user_id"] != uid:
            raise HTTPException(403, "无权删除")
        conn.execute("DELETE FROM reports WHERE id=?", (rid,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


# ---------------- 博客 / 笔记（列表/详情公开；写改删需登录且仅限本人） ----------------
@app.get("/api/blogs")
def list_blogs(request: Request):
    scope = request.query_params.get("scope")
    uid = get_uid(request.headers.get("Authorization", ""), request)
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM blogs ORDER BY created_at DESC").fetchall()
    finally:
        conn.close()
    blogs = [blog_public(b) for b in rows]
    # 匿名用户只能看 public（即便显式带 scope=public 也强制过滤 private）
    if scope == "public" or not uid:
        blogs = [b for b in blogs if b["visibility"] != "private"]
    return blogs


@app.get("/api/blogs/{bid}")
def blog_detail(bid: str, request: Request):
    uid = get_uid(request.headers.get("Authorization", ""), request)
    conn = get_conn()
    try:
        b = conn.execute("SELECT * FROM blogs WHERE id=?", (bid,)).fetchone()
    finally:
        conn.close()
    if not b:
        raise HTTPException(404, "not found")
    # private 博客对非登录用户不可见（避免匿名越权读取）
    if b["visibility"] == "private" and not uid:
        raise HTTPException(404, "not found")
    return {
        "id": b["id"], "title": b["title"], "summary": b["summary"], "content": b["content"],
        "category": b["category"], "tags": json.loads(b["tags"] or "[]"), "visibility": b["visibility"] or "public",
        "cover": b["cover"], "authorId": b["author_id"], "authorName": b["author_name"],
        "createdAt": b["created_at"], "updatedAt": b["updated_at"],
    }


@app.post("/api/blogs")
async def create_blog(body: dict, uid: str = Depends(require_uid)):
    title = (body.get("title") or "").strip()
    content = (body.get("content") or "").strip()
    if not title:
        raise HTTPException(400, "标题不能为空")
    if not content:
        raise HTTPException(400, "正文不能为空")
    conn = get_conn()
    try:
        u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        author_name = u["username"] if u else ""
    finally:
        conn.close()
    now = int(time.time() * 1000)
    tags = body.get("tags")
    if not isinstance(tags, list):
        tags = []
    b_id = uid_now()
    with write_lock:
        conn = get_conn()
        try:
            conn.execute(
                "INSERT INTO blogs (id, author_id, author_name, title, summary, content, category, tags, visibility, cover, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (b_id, uid, author_name, title, (body.get("summary") or "").strip(), content,
                 (body.get("category") or "").strip(), json.dumps([str(t).strip() for t in tags if str(t).strip()], ensure_ascii=False),
                 "private" if body.get("visibility") == "private" else "public", (body.get("cover") or "").strip(), now, now),
            )
            conn.commit()
        finally:
            conn.close()
    return {"id": b_id, "title": title, "summary": body.get("summary", ""), "content": content,
            "category": body.get("category", ""), "tags": [str(t).strip() for t in tags if str(t).strip()],
            "visibility": "private" if body.get("visibility") == "private" else "public", "cover": body.get("cover", ""),
            "authorId": uid, "authorName": author_name, "createdAt": now, "updatedAt": now}


@app.put("/api/blogs/{bid}")
async def update_blog(bid: str, body: dict, uid: str = Depends(require_uid)):
    with write_lock:
        conn = get_conn()
        try:
            b = conn.execute("SELECT * FROM blogs WHERE id=?", (bid,)).fetchone()
            if not b:
                raise HTTPException(404, "not found")
            if b["author_id"] != uid:
                raise HTTPException(403, "只能修改/删除自己的博客")
            title = (body["title"].strip() if body.get("title") is not None else b["title"]) or b["title"]
            summary = body["summary"].strip() if body.get("summary") is not None else b["summary"]
            content = body["content"] if body.get("content") is not None else b["content"]
            category = body["category"].strip() if body.get("category") is not None else b["category"]
            cover = body["cover"].strip() if body.get("cover") is not None else b["cover"]
            visibility = "private" if body.get("visibility") == "private" else "public"
            tags = body.get("tags")
            if isinstance(tags, list):
                tags = json.dumps([str(t).strip() for t in tags if str(t).strip()], ensure_ascii=False)
            else:
                tags = b["tags"]
            conn.execute(
                "UPDATE blogs SET title=?, summary=?, content=?, category=?, tags=?, visibility=?, cover=?, updated_at=? WHERE id=?",
                (title, summary, content, category, tags, visibility, cover, int(time.time() * 1000), bid),
            )
            conn.commit()
        finally:
            conn.close()
    return {"id": bid, "title": title, "summary": summary, "content": content, "category": category,
            "tags": json.loads(tags), "visibility": visibility, "cover": cover,
            "authorId": b["author_id"], "authorName": b["author_name"],
            "createdAt": b["created_at"], "updatedAt": int(time.time() * 1000)}


@app.delete("/api/blogs/{bid}")
def delete_blog(bid: str, uid: str = Depends(require_uid)):
    conn = get_conn()
    try:
        b = conn.execute("SELECT * FROM blogs WHERE id=?", (bid,)).fetchone()
        if not b:
            raise HTTPException(404, "not found")
        if b["author_id"] != uid:
            raise HTTPException(403, "只能修改/删除自己的博客")
        conn.execute("DELETE FROM blogs WHERE id=?", (bid,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


# ---------------- 财务工作台（按用户隔离：每个登录账号独立数据） ----------------
@app.get("/api/finance/items")
def list_finance(uid: str = Depends(require_uid)):
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM finance_items WHERE user_id=?", (uid,)).fetchall()
        meta = conn.execute("SELECT initialized FROM finance_meta WHERE user_id=?", (uid,)).fetchone()
    finally:
        conn.close()
    items = {}
    for r in rows:
        dk = r["date_key"]
        items.setdefault(dk, []).append({
            "id": r["id"], "title": r["title"], "cat": r["cat"], "pri": r["pri"],
            "done": bool(r["done"]), "note": r["note"], "owner": r["owner"], "time": r["time"],
        })
    return {"items": items, "initialized": bool(meta["initialized"]) if meta else False}


@app.put("/api/finance/items")
async def put_finance(body: dict, uid: str = Depends(require_uid)):
    raw = body if isinstance(body, dict) else {}
    items = raw.get("items") if isinstance(raw, dict) and "items" in raw else raw
    if not isinstance(items, dict):
        items = {}
    now = int(time.time() * 1000)
    with write_lock:
        conn = get_conn()
        try:
            conn.execute("DELETE FROM finance_items WHERE user_id=?", (uid,))
            for dk, arr in items.items():
                if not isinstance(arr, list):
                    continue
                for it in arr:
                    if not isinstance(it, dict):
                        continue
                    iid = it.get("id") or uid_now()
                    conn.execute(
                        "INSERT INTO finance_items (id,user_id,date_key,title,cat,pri,done,note,owner,time,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (iid, uid, str(dk), (it.get("title") or ""), (it.get("cat") or ""),
                         (it.get("pri") or "中"), 1 if it.get("done") else 0, (it.get("note") or ""),
                         (it.get("owner") or ""), (it.get("time") or ""), now, now),
                    )
            conn.execute("INSERT OR REPLACE INTO finance_meta (user_id, initialized) VALUES (?,1)", (uid,))
            conn.commit()
        finally:
            conn.close()
    return {"ok": True}


# ---------------- 报税工作台（按用户隔离：每个登录账号独立数据） ----------------
# 数据字段沿用参考应用：公司名称 / 税种事项 / 状态 / 截止日期 / 所属月份 / 备注
@app.get("/api/tax/items")
def list_tax(uid: str = Depends(require_uid)):
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id,company,tax_item,status,deadline,month,note FROM tax_items WHERE user_id=? ORDER BY deadline ASC",
            (uid,),
        ).fetchall()
    finally:
        conn.close()
    items = [
        {
            "_id": r["id"],
            "公司名称": r["company"],
            "税种事项": r["tax_item"],
            "状态": r["status"],
            "截止日期": r["deadline"],
            "所属月份": r["month"],
            "备注": r["note"],
        }
        for r in rows
    ]
    return {"items": items}


@app.post("/api/tax/items")
async def create_tax(body: dict, uid: str = Depends(require_uid)):
    b = body if isinstance(body, dict) else {}
    company = (b.get("company") or "").strip()
    tax_item = (b.get("tax_item") or "").strip()
    if not company or not tax_item:
        return JSONResponse(status_code=400, content={"error": "公司名称与税种事项必填"})
    now = int(time.time() * 1000)
    with write_lock:
        conn = get_conn()
        try:
            cur = conn.execute(
                "INSERT INTO tax_items (user_id,company,tax_item,status,deadline,month,note,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (uid, company, tax_item, (b.get("status") or "未处理"), (b.get("deadline") or ""),
                 (b.get("month") or ""), (b.get("note") or ""), now, now),
            )
            iid = cur.lastrowid
            conn.commit()
        finally:
            conn.close()
    return {"_id": iid, "公司名称": company, "税种事项": tax_item, "状态": b.get("status") or "未处理",
            "截止日期": b.get("deadline") or "", "所属月份": b.get("month") or "", "备注": b.get("note") or ""}


@app.put("/api/tax/items/{item_id}")
async def update_tax(item_id: int, body: dict, uid: str = Depends(require_uid)):
    b = body if isinstance(body, dict) else {}
    with write_lock:
        conn = get_conn()
        try:
            exists = conn.execute(
                "SELECT id FROM tax_items WHERE id=? AND user_id=?", (item_id, uid)
            ).fetchone()
            if not exists:
                return JSONResponse(status_code=404, content={"error": "记录不存在"})
            conn.execute(
                "UPDATE tax_items SET company=?,tax_item=?,status=?,deadline=?,month=?,note=?,updated_at=? WHERE id=? AND user_id=?",
                ((b.get("company") or "").strip(), (b.get("tax_item") or "").strip(), (b.get("status") or "未处理"),
                 (b.get("deadline") or ""), (b.get("month") or ""), (b.get("note") or ""),
                 int(time.time() * 1000), item_id, uid),
            )
            conn.commit()
        finally:
            conn.close()
    return {"ok": True}


@app.delete("/api/tax/items/{item_id}")
def delete_tax(item_id: int, uid: str = Depends(require_uid)):
    with write_lock:
        conn = get_conn()
        try:
            cur = conn.execute("DELETE FROM tax_items WHERE id=? AND user_id=?", (item_id, uid))
            conn.commit()
            if cur.rowcount == 0:
                return JSONResponse(status_code=404, content={"error": "记录不存在或无权删除"})
        finally:
            conn.close()
    return {"ok": True}


# ---------------- 买房计算器 API ----------------
@app.get("/api/mortgage/regions")
def list_mortgage_regions(uid: str = Depends(require_uid)):
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT key,name,lpr5,commercial_first,commercial_second,fund_first,fund_second,down_first,down_second,fund_cap_single,fund_cap_double FROM mortgage_regions WHERE enabled=1 ORDER BY sort ASC"
        ).fetchall()
    finally:
        conn.close()
    return {"regions": [
        {
            "key": r["key"], "name": r["name"],
            "lpr5": float(r["lpr5"]),
            "commercial_first": float(r["commercial_first"]),
            "commercial_second": float(r["commercial_second"]),
            "fund_first": float(r["fund_first"]),
            "fund_second": float(r["fund_second"]),
            "down_first": float(r["down_first"]),
            "down_second": float(r["down_second"]),
            "fund_cap_single": float(r["fund_cap_single"]),
            "fund_cap_double": float(r["fund_cap_double"]),
        }
        for r in rows
    ]}


@app.get("/api/mortgage/scenarios")
def list_mortgage_scenarios(uid: str = Depends(require_uid)):
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id,name,params,result,created_at FROM mortgage_scenarios WHERE user_id=? ORDER BY updated_at DESC",
            (uid,),
        ).fetchall()
    finally:
        conn.close()
    return {"scenarios": [
        {"id": r["id"], "name": r["name"], "params": json.loads(r["params"] or "{}"),
         "result": json.loads(r["result"] or "{}"), "created_at": r["created_at"]}
        for r in rows
    ]}


@app.post("/api/mortgage/scenarios")
async def create_mortgage_scenario(body: dict, uid: str = Depends(require_uid)):
    b = body if isinstance(body, dict) else {}
    name = (b.get("name") or "未命名方案").strip() or "未命名方案"
    params = b.get("params") if isinstance(b.get("params"), dict) else {}
    result = b.get("result") if isinstance(b.get("result"), dict) else {}
    now = int(time.time() * 1000)
    with write_lock:
        conn = get_conn()
        try:
            cur = conn.execute(
                "INSERT INTO mortgage_scenarios (user_id,name,params,result,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                (uid, name, json.dumps(params, ensure_ascii=False), json.dumps(result, ensure_ascii=False), now, now),
            )
            sid = cur.lastrowid
            conn.commit()
        finally:
            conn.close()
    return {"id": sid, "name": name, "ok": True}


@app.put("/api/mortgage/scenarios/{sid}")
async def update_mortgage_scenario(sid: int, body: dict, uid: str = Depends(require_uid)):
    b = body if isinstance(body, dict) else {}
    with write_lock:
        conn = get_conn()
        try:
            exists = conn.execute("SELECT id FROM mortgage_scenarios WHERE id=? AND user_id=?", (sid, uid)).fetchone()
            if not exists:
                return JSONResponse(status_code=404, content={"error": "方案不存在"})
            conn.execute(
                "UPDATE mortgage_scenarios SET name=?,params=?,result=?,updated_at=? WHERE id=? AND user_id=?",
                ((b.get("name") or "").strip(), json.dumps(b.get("params") or {}, ensure_ascii=False),
                 json.dumps(b.get("result") or {}, ensure_ascii=False), int(time.time() * 1000), sid, uid),
            )
            conn.commit()
        finally:
            conn.close()
    return {"ok": True}


@app.delete("/api/mortgage/scenarios/{sid}")
def delete_mortgage_scenario(sid: int, uid: str = Depends(require_uid)):
    with write_lock:
        conn = get_conn()
        try:
            cur = conn.execute("DELETE FROM mortgage_scenarios WHERE id=? AND user_id=?", (sid, uid))
            conn.commit()
            if cur.rowcount == 0:
                return JSONResponse(status_code=404, content={"error": "方案不存在或无权删除"})
        finally:
            conn.close()
    return {"ok": True}


# ---------------- 应用中心 / 设置 / 管理后台 API ----------------
def app_row_to_dict(r):
    return {
        "key": r["key"], "name": r["name"], "icon": r["icon"], "desc": r["desc"],
        "entry": r["entry"], "color": r["color"], "enabled": bool(r["enabled"]),
        "intro": r["intro"], "sort": r["sort"],
    }


@app.get("/api/apps")
def list_visible_apps(authorization: str = Header(default="")):
    """当前用户可见的应用。已登录按权限过滤（未显式收回的已启用应用均可见）；匿名返回全部已启用应用（预览）。"""
    uid = get_uid(authorization)
    conn = get_conn()
    try:
        if uid:
            rows = conn.execute(
                "SELECT c.* FROM app_catalog c LEFT JOIN user_app_perm p ON p.app_key=c.key AND p.user_id=? "
                "WHERE c.enabled=1 AND (p.granted IS NULL OR p.granted=1) ORDER BY c.sort ASC, c.name ASC",
                (uid,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM app_catalog WHERE enabled=1 ORDER BY sort ASC, name ASC"
            ).fetchall()
    finally:
        conn.close()
    return {"apps": [app_row_to_dict(r) for r in rows]}


@app.get("/api/settings")
def get_settings():
    conn = get_conn()
    try:
        rows = conn.execute("SELECT key,value FROM settings_meta").fetchall()
    finally:
        conn.close()
    return {"settings": {r["key"]: r["value"] for r in rows}}


# ---------- 应用管理 ----------
@app.get("/api/admin/apps")
def admin_list_apps(_: str = Depends(require_admin)):
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM app_catalog ORDER BY sort ASC, name ASC").fetchall()
    finally:
        conn.close()
    return {"apps": [app_row_to_dict(r) for r in rows]}


@app.post("/api/admin/apps")
async def admin_create_app(body: dict, _: str = Depends(require_admin)):
    key = (body.get("key") or "").strip()
    name = (body.get("name") or "").strip()
    if not re.match(r"^[a-zA-Z0-9_-]{2,32}$", key):
        raise HTTPException(400, "应用标识需为 2-32 位字母/数字/下划线/连字符")
    if not name:
        raise HTTPException(400, "应用名称必填")
    conn = get_conn()
    try:
        if conn.execute("SELECT 1 FROM app_catalog WHERE key=?", (key,)).fetchone():
            raise HTTPException(409, "该应用标识已存在")
        now = int(time.time() * 1000)
        conn.execute(
            "INSERT INTO app_catalog (id,key,name,icon,desc,entry,color,sort,enabled,intro,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (uid_now(), key, name, (body.get("icon") or "").strip(), (body.get("desc") or "").strip(),
             (body.get("entry") or "").strip(), (body.get("color") or "").strip(), int(body.get("sort", 0) or 0),
             1 if body.get("enabled", True) else 0, (body.get("intro") or "").strip(), now, now),
        )
        conn.commit()
    finally:
        conn.close()
    grant_app_to_all_users(key)
    return {"ok": True, "key": key}


@app.put("/api/admin/apps/{key}")
async def admin_update_app(key: str, body: dict, _: str = Depends(require_admin)):
    conn = get_conn()
    try:
        cur = conn.execute("SELECT * FROM app_catalog WHERE key=?", (key,)).fetchone()
        if not cur:
            raise HTTPException(404, "应用不存在")
        conn.execute(
            "UPDATE app_catalog SET name=?, icon=?, desc=?, entry=?, color=?, sort=?, enabled=?, intro=?, updated_at=? WHERE key=?",
            ((body.get("name") or cur["name"]).strip(), body.get("icon", cur["icon"]), body.get("desc", cur["desc"]),
             body.get("entry", cur["entry"]), body.get("color", cur["color"]), int(body.get("sort", cur["sort"]) or 0),
             1 if body.get("enabled", cur["enabled"]) else 0, body.get("intro", cur["intro"]), int(time.time() * 1000), key),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@app.delete("/api/admin/apps/{key}")
def admin_delete_app(key: str, _: str = Depends(require_admin)):
    with write_lock:
        conn = get_conn()
        try:
            conn.execute("DELETE FROM app_catalog WHERE key=?", (key,))
            conn.execute("DELETE FROM user_app_perm WHERE app_key=?", (key,))
            conn.commit()
        finally:
            conn.close()
    return {"ok": True}


# ---------- 用户管理 ----------
@app.get("/api/admin/users")
def admin_list_users(_: str = Depends(require_admin)):
    conn = get_conn()
    try:
        rows = conn.execute("SELECT id,username,display_name,role,created_at FROM users ORDER BY created_at ASC").fetchall()
    finally:
        conn.close()
    return {"users": [{"id": r["id"], "username": r["username"], "displayName": r["display_name"],
                       "role": r["role"], "createdAt": r["created_at"]} for r in rows]}


@app.post("/api/admin/users")
async def admin_create_user(body: dict, _: str = Depends(require_admin)):
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not (3 <= len(username) <= 20):
        raise HTTPException(400, "用户名需 3-20 位")
    if len(password) < 6:
        raise HTTPException(400, "密码至少 6 位")
    role = body.get("role") if body.get("role") in ("admin", "user") else "user"
    display_name = (body.get("displayName") or "").strip()
    conn = get_conn()
    try:
        if conn.execute("SELECT 1 FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone():
            raise HTTPException(409, "该用户名已被注册")
        hp = hash_password(password)
        u_id = uid_now()
        now = int(time.time() * 1000)
        conn.execute(
            "INSERT INTO users (id,username,salt,hash,created_at,role,display_name) VALUES (?,?,?,?,?,?,?)",
            (u_id, username, hp["salt"], hp["hash"], now, role, display_name),
        )
        conn.commit()
    finally:
        conn.close()
    grant_all_apps(u_id)
    return {"ok": True, "id": u_id}


@app.put("/api/admin/users/{uid}")
async def admin_update_user(uid: str, body: dict, _: str = Depends(require_admin)):
    with write_lock:
        conn = get_conn()
        try:
            u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            if not u:
                raise HTTPException(404, "用户不存在")
            role = body.get("role", u["role"])
            if role not in ("admin", "user"):
                role = u["role"]
            display_name = (body.get("displayName") if body.get("displayName") is not None else u["display_name"])
            pw = body.get("password")
            if pw:
                if len(pw) < 6:
                    raise HTTPException(400, "密码至少 6 位")
                hp = hash_password(pw)
                conn.execute(
                    "UPDATE users SET role=?, display_name=?, salt=?, hash=? WHERE id=?",
                    (role, display_name, hp["salt"], hp["hash"], uid),
                )
            else:
                conn.execute(
                    "UPDATE users SET role=?, display_name=? WHERE id=?",
                    (role, display_name, uid),
                )
            conn.commit()
        finally:
            conn.close()
    return {"ok": True}


@app.delete("/api/admin/users/{uid}")
def admin_delete_user(uid: str, _: str = Depends(require_admin)):
    with write_lock:
        conn = get_conn()
        try:
            u = conn.execute("SELECT role FROM users WHERE id=?", (uid,)).fetchone()
            if not u:
                raise HTTPException(404, "用户不存在")
            if u["role"] == "admin":
                adm = conn.execute("SELECT COUNT(*) AS c FROM users WHERE role='admin'").fetchone()["c"]
                if adm <= 1:
                    raise HTTPException(400, "至少保留一个管理员账号")
            conn.execute("DELETE FROM user_app_perm WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM reports WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM blogs WHERE author_id=?", (uid,))
            conn.execute("DELETE FROM finance_items WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM finance_meta WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM tax_items WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM users WHERE id=?", (uid,))
            conn.commit()
        finally:
            conn.close()
    return {"ok": True}


# ---------- 权限管理（用户 ↔ 应用） ----------
@app.get("/api/admin/permissions")
def admin_permissions(_: str = Depends(require_admin)):
    conn = get_conn()
    try:
        users = conn.execute("SELECT id,username,display_name,role FROM users ORDER BY created_at ASC").fetchall()
        apps = conn.execute("SELECT key,name,icon FROM app_catalog ORDER BY sort ASC, name ASC").fetchall()
        perms = conn.execute("SELECT user_id,app_key,granted FROM user_app_perm").fetchall()
    finally:
        conn.close()
    grants = {}
    for p in perms:
        grants.setdefault(p["user_id"], {})[p["app_key"]] = bool(p["granted"])
    return {
        "users": [{"id": u["id"], "username": u["username"], "displayName": u["display_name"], "role": u["role"]} for u in users],
        "apps": [{"key": a["key"], "name": a["name"], "icon": a["icon"]} for a in apps],
        "grants": grants,
    }


@app.put("/api/admin/permissions")
async def admin_set_permission(body: dict, _: str = Depends(require_admin)):
    user_id = body.get("user_id") or ""
    app_key = body.get("app_key") or ""
    granted = bool(body.get("granted", True))
    if not user_id or not app_key:
        raise HTTPException(400, "缺少 user_id 或 app_key")
    with write_lock:
        conn = get_conn()
        try:
            if not conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
                raise HTTPException(404, "用户不存在")
            if not conn.execute("SELECT 1 FROM app_catalog WHERE key=?", (app_key,)).fetchone():
                raise HTTPException(404, "应用不存在")
            conn.execute(
                "INSERT OR REPLACE INTO user_app_perm (user_id, app_key, granted) VALUES (?,?,?)",
                (user_id, app_key, 1 if granted else 0),
            )
            conn.commit()
        finally:
            conn.close()
    return {"ok": True}


@app.put("/api/admin/settings")
async def admin_set_settings(body: dict, _: str = Depends(require_admin)):
    payload = body.get("settings") if isinstance(body.get("settings"), dict) else body
    with write_lock:
        conn = get_conn()
        try:
            for k, v in payload.items():
                if not isinstance(k, str):
                    continue
                conn.execute("INSERT OR REPLACE INTO settings_meta (key, value) VALUES (?, ?)", (k, str(v)))
            conn.commit()
        finally:
            conn.close()
    return {"ok": True}


@app.get("/api/admin/mortgage-regions")
def admin_list_regions(_: str = Depends(require_admin)):
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT key,name,lpr5,commercial_first,commercial_second,fund_first,fund_second,down_first,down_second,fund_cap_single,fund_cap_double,enabled,sort FROM mortgage_regions ORDER BY sort ASC"
        ).fetchall()
    finally:
        conn.close()
    return {"regions": [
        {
            "key": r["key"], "name": r["name"],
            "lpr5": float(r["lpr5"]),
            "commercial_first": float(r["commercial_first"]),
            "commercial_second": float(r["commercial_second"]),
            "fund_first": float(r["fund_first"]),
            "fund_second": float(r["fund_second"]),
            "down_first": float(r["down_first"]),
            "down_second": float(r["down_second"]),
            "fund_cap_single": float(r["fund_cap_single"]),
            "fund_cap_double": float(r["fund_cap_double"]),
            "enabled": bool(r["enabled"]),
            "sort": r["sort"],
        }
        for r in rows
    ]}


@app.put("/api/admin/mortgage-regions/{key}")
async def admin_update_region(key: str, body: dict, _: str = Depends(require_admin)):
    fields = {
        "lpr5": "lpr5", "commercial_first": "commercial_first", "commercial_second": "commercial_second",
        "fund_first": "fund_first", "fund_second": "fund_second", "down_first": "down_first",
        "down_second": "down_second", "fund_cap_single": "fund_cap_single", "fund_cap_double": "fund_cap_double",
        "enabled": "enabled", "sort": "sort",
    }
    sets, vals = [], []
    for k, col in fields.items():
        if k in body:
            sets.append(f"{col}=?")
            v = body[k]
            vals.append(float(v) if k not in ("enabled", "sort") else (1 if v else 0) if k == "enabled" else int(v))
    if not sets:
        return {"ok": True}
    vals.append(key)
    with write_lock:
        conn = get_conn()
        try:
            conn.execute(f"UPDATE mortgage_regions SET {','.join(sets)} WHERE key=?", vals)
            conn.commit()
        finally:
            conn.close()
    return {"ok": True}


# ---------------- 静态资源（前端单页，public/ 目录） ----------------
@app.get("/")
def index():
    return FileResponse(str(PUBLIC_DIR / "index.html"))


app.mount("/", StaticFiles(directory=str(PUBLIC_DIR), html=True), name="static")
