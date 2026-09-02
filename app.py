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
from fastapi.responses import JSONResponse, FileResponse
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
            """
        )
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


def get_uid(authorization: str = Header(default="")) -> str:
    m = re.match(r"^Bearer\s+(.+)$", authorization or "", re.I)
    if not m:
        return None
    return verify_token(m.group(1))


def require_uid(authorization: str = Header(default="")) -> str:
    uid = get_uid(authorization)
    if not uid:
        raise HTTPException(401, "未登录")
    return uid


def uid_now() -> str:
    return base64.urlsafe_b64encode(os.urandom(9)).rstrip(b"=").decode("ascii") + secrets.token_hex(3)


def row_to_user(r) -> dict:
    return {"id": r["id"], "username": r["username"]}


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


@app.get("/api/health")
def health():
    return {"ok": True, "time": int(time.time() * 1000)}


# ---------------- 认证 ----------------
@app.post("/api/auth/register")
async def register(body: dict):
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
    return {"token": sign_token(u_id), "user": {"id": u_id, "username": username}}


@app.post("/api/auth/login")
async def login(body: dict):
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    conn = get_conn()
    try:
        u = conn.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone()
    finally:
        conn.close()
    if not u or not verify_password(password, u["salt"], u["hash"]):
        raise HTTPException(401, "用户名或密码错误")
    return {"token": sign_token(u["id"]), "user": row_to_user(u)}


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
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM blogs ORDER BY created_at DESC").fetchall()
    finally:
        conn.close()
    blogs = [blog_public(b) for b in rows]
    if scope == "public":
        blogs = [b for b in blogs if b["visibility"] != "private"]
    return blogs


@app.get("/api/blogs/{bid}")
def blog_detail(bid: str):
    conn = get_conn()
    try:
        b = conn.execute("SELECT * FROM blogs WHERE id=?", (bid,)).fetchone()
    finally:
        conn.close()
    if not b:
        raise HTTPException(404, "not found")
    return {
        "id": b["id"], "title": b["title"], "summary": b["summary"], "content": b["content"],
        "category": b["category"], "tags": json.loads(b["tags"] or "[]"), "visibility": b["visibility"] or "public",
        "cover": b["cover"], "authorId": b["author_id"], "authorName": b["author_name"],
        "createdAt": b["created_at"], "updatedAt": b["updatedAt"],
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


# ---------------- 静态资源（前端单页，public/ 目录） ----------------
@app.get("/")
def index():
    return FileResponse(str(PUBLIC_DIR / "index.html"))


app.mount("/", StaticFiles(directory=str(PUBLIC_DIR), html=True), name="static")
