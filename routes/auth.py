# -*- coding: utf-8 -*-
"""认证 / 会话 —— APIRouter。拆分自原 app.py，逻辑/鉴权/返回结构与拆分前完全一致。"""
import json
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

# ---------------- 认证 ----------------
@router.post("/api/auth/register")
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


@router.post("/api/auth/login")
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


@router.post("/api/auth/logout")
async def logout(response: Response):
    clear_auth_cookie(response)
    return {"ok": True}


@router.get("/api/auth/me")
def me(uid: str = Depends(require_uid)):
    conn = get_conn()
    try:
        u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    finally:
        conn.close()
    if not u:
        raise HTTPException(401, "用户不存在")
    return {"user": row_to_user(u)}
