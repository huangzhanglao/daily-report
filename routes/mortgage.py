# -*- coding: utf-8 -*-
"""买房计算器 —— APIRouter。拆分自原 app.py，逻辑/鉴权/返回结构与拆分前完全一致。"""
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

# ---------------- 买房计算器 API ----------------
@router.get("/api/mortgage/regions")
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


@router.get("/api/mortgage/scenarios")
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


@router.post("/api/mortgage/scenarios")
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


@router.put("/api/mortgage/scenarios/{sid}")
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


@router.delete("/api/mortgage/scenarios/{sid}")
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
