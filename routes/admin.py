# -*- coding: utf-8 -*-
"""应用中心 / 设置 / 管理后台 —— APIRouter。拆分自原 app.py，逻辑/鉴权/返回结构与拆分前完全一致。"""
import json
import re
import time
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Header, Response

# 共享基础设施（按需使用）
from core import (
    get_conn, write_lock, uid_now, row_to_user, blog_public,
    get_uid, require_uid, require_admin,
    sign_token, verify_password, hash_password,
    set_auth_cookie, clear_auth_cookie, AUTH_COOKIE,
    rate_limit, client_ip, public_register_enabled,
    grant_all_apps, grant_app_to_all_users,
)

router = APIRouter()

# ---------------- 应用中心 / 设置 / 管理后台 API ----------------
def app_row_to_dict(r):
    return {
        "key": r["key"], "name": r["name"], "icon": r["icon"], "desc": r["desc"],
        "entry": r["entry"], "color": r["color"], "enabled": bool(r["enabled"]),
        "intro": r["intro"], "sort": r["sort"],
    }


@router.get("/api/apps")
def list_visible_apps(uid: str = Depends(require_uid)):
    """当前登录用户可见的应用（按权限过滤）。需登录，避免匿名暴露模块清单。"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT c.* FROM app_catalog c LEFT JOIN user_app_perm p ON p.app_key=c.key AND p.user_id=? "
            "WHERE c.enabled=1 AND (p.granted IS NULL OR p.granted=1) ORDER BY c.sort ASC, c.name ASC",
            (uid,),
        ).fetchall()
    finally:
        conn.close()
    return {"apps": [app_row_to_dict(r) for r in rows]}


@router.get("/api/settings")
def get_settings(authorization: str = Header(default=""), request: Request = None):
    uid = get_uid(authorization, request)
    conn = get_conn()
    try:
        rows = conn.execute("SELECT key,value FROM settings_meta").fetchall()
    finally:
        conn.close()
    settings = {r["key"]: r["value"] for r in rows}
    if not uid:
        # 匿名仅暴露 allow_register（注册流程需判断是否开放注册），其余设置需登录后可见
        return {"settings": {"allow_register": settings.get("allow_register", "0")}}
    return {"settings": settings}


# ---------- 应用管理 ----------
@router.get("/api/admin/apps")
def admin_list_apps(_: str = Depends(require_admin)):
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM app_catalog ORDER BY sort ASC, name ASC").fetchall()
    finally:
        conn.close()
    return {"apps": [app_row_to_dict(r) for r in rows]}


@router.post("/api/admin/apps")
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


@router.put("/api/admin/apps/{key}")
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


@router.delete("/api/admin/apps/{key}")
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
@router.get("/api/admin/users")
def admin_list_users(_: str = Depends(require_admin)):
    conn = get_conn()
    try:
        rows = conn.execute("SELECT id,username,display_name,role,created_at FROM users ORDER BY created_at ASC").fetchall()
    finally:
        conn.close()
    return {"users": [{"id": r["id"], "username": r["username"], "displayName": r["display_name"],
                       "role": r["role"], "createdAt": r["created_at"]} for r in rows]}


@router.post("/api/admin/users")
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


@router.put("/api/admin/users/{uid}")
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


@router.delete("/api/admin/users/{uid}")
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
@router.get("/api/admin/permissions")
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


@router.put("/api/admin/permissions")
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


@router.put("/api/admin/settings")
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


@router.get("/api/admin/mortgage-regions")
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


@router.put("/api/admin/mortgage-regions/{key}")
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
