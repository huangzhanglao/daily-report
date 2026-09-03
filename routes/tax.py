# -*- coding: utf-8 -*-
"""报税工作台 —— APIRouter。拆分自原 app.py，逻辑/鉴权/返回结构与拆分前完全一致。"""
import json
import time
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Header, Response
from fastapi.responses import JSONResponse

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

# ---------------- 报税工作台（按用户隔离：每个登录账号独立数据） ----------------
# 数据字段沿用参考应用：公司名称 / 税种事项 / 状态 / 截止日期 / 所属月份 / 备注
@router.get("/api/tax/items")
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


@router.post("/api/tax/items")
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


@router.put("/api/tax/items/{item_id}")
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


@router.delete("/api/tax/items/{item_id}")
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
