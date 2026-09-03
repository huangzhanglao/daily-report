# -*- coding: utf-8 -*-
"""财务工作台 —— APIRouter。拆分自原 app.py，逻辑/鉴权/返回结构与拆分前完全一致。"""
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

# ---------------- 财务工作台（按用户隔离：每个登录账号独立数据） ----------------
@router.get("/api/finance/items")
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


@router.put("/api/finance/items")
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
