# -*- coding: utf-8 -*-
"""共享基础设施层。

被拆分自原单文件 app.py。供 app.py 与 routes/*.py 共同 import：
- 路径/密钥/DB：ROOT, PUBLIC_DIR, DATA_DIR, DB_FILE, SECRET, get_conn, write_lock
- 建表/迁移/种子：init_db, migrate_from_json, seed_app_catalog, seed_mortgage_regions,
  grant_all_apps, grant_app_to_all_users, DEFAULT_APPS, DEFAULT_SETTINGS, DEFAULT_REGIONS
- 密码/token：hash_password, verify_password, sign_token, verify_token
- 鉴权依赖：get_uid, require_uid, require_admin, set_auth_cookie, clear_auth_cookie, AUTH_COOKIE
- 限速/工具：rate_limit, client_ip, public_register_enabled, uid_now
- 行映射：row_to_user, blog_public

注意：本模块只定义，不在 import 时执行建表/种子（由 app.py 装配层显式触发）。
"""

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

from fastapi import HTTPException, Header, Request

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
            CREATE TABLE IF NOT EXISTS ledger_records (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                date TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'weight',
                channel TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT '',
                amount REAL NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ledger_user_date ON ledger_records(user_id, date);
            CREATE TABLE IF NOT EXISTS llm_providers (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                base_url TEXT NOT NULL DEFAULT '',
                api_key TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                is_default INTEGER NOT NULL DEFAULT 0,
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
    {"key": "ledger", "name": "记账本工作台", "icon": "📒",
     "desc": "记录每日体重、微信 / 支付宝收支，自动汇总本月收入、支出、结余与消费渠道占比，并绘制体重趋势。数据按账号隔离。",
     "entry": "ledger.html", "color": "linear-gradient(135deg,#0ea5e9,#22d3ee)", "sort": 6},
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
