# -*- coding: utf-8 -*-
"""记账本工作台 —— APIRouter。拆分自原 app.py，逻辑/鉴权/返回结构与拆分前完全一致。"""
import json
import re
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

# ---------------- 记账本工作台 API ----------------
# 单表 ledger_records：kind 区分 weight / income / expense；amount 对 weight 表示公斤数。
def _month_filter(month: str):
    if month and re.match(r"^\d{4}-\d{2}$", month):
        return " AND date LIKE ?", (month + "%",)
    return "", ()

def _ledger_row(r) -> dict:
    return {
        "id": r["id"], "date": r["date"], "kind": r["kind"],
        "channel": r["channel"], "category": r["category"],
        "amount": r["amount"], "note": r["note"],
        "createdAt": r["created_at"], "updatedAt": r["updated_at"],
    }

@router.get("/api/ledger/records")
def list_ledger(uid: str = Depends(require_uid), month: str = "", date: str = ""):
    conn = get_conn()
    try:
        if date:
            rows = conn.execute(
                "SELECT * FROM ledger_records WHERE user_id=? AND date=? ORDER BY created_at DESC", (uid, date)
            ).fetchall()
        else:
            mf, mp = _month_filter(month)
            rows = conn.execute(
                "SELECT * FROM ledger_records WHERE user_id=? " + mf + " ORDER BY date DESC, created_at DESC",
                (uid,) + mp,
            ).fetchall()
    finally:
        conn.close()
    return {"records": [_ledger_row(r) for r in rows]}

@router.post("/api/ledger/records")
async def create_ledger(body: dict, uid: str = Depends(require_uid)):
    date = (body.get("date") or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise HTTPException(400, "日期格式应为 YYYY-MM-DD")
    kind = body.get("kind") or "expense"
    if kind not in ("weight", "income", "expense"):
        raise HTTPException(400, "kind 不合法")
    try:
        amount = float(body.get("amount") or 0)
    except Exception:
        raise HTTPException(400, "金额/体重必须为数字")
    if amount < 0:
        raise HTTPException(400, "金额/体重不能为负")
    now = int(time.time() * 1000)
    rid = uid_now()
    with write_lock:
        conn = get_conn()
        try:
            conn.execute(
                "INSERT INTO ledger_records (id,user_id,date,kind,channel,category,amount,note,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (rid, uid, date, kind, (body.get("channel") or "").strip(),
                 (body.get("category") or "").strip(), amount, (body.get("note") or "").strip(), now, now),
            )
            conn.commit()
        finally:
            conn.close()
    return {"ok": True, "record": {"id": rid, "date": date, "kind": kind,
                                   "channel": body.get("channel", ""), "category": body.get("category", ""),
                                   "amount": amount, "note": body.get("note", "")}}

@router.put("/api/ledger/records/{rid}")
async def update_ledger(rid: str, body: dict, uid: str = Depends(require_uid)):
    with write_lock:
        conn = get_conn()
        try:
            r = conn.execute("SELECT * FROM ledger_records WHERE id=? AND user_id=?", (rid, uid)).fetchone()
            if not r:
                raise HTTPException(404, "记录不存在")
            date = (body.get("date") or r["date"]).strip()
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
                raise HTTPException(400, "日期格式应为 YYYY-MM-DD")
            kind = body.get("kind") or r["kind"]
            if kind not in ("weight", "income", "expense"):
                raise HTTPException(400, "kind 不合法")
            try:
                amount = float(body.get("amount") if body.get("amount") is not None else r["amount"])
            except Exception:
                raise HTTPException(400, "金额/体重必须为数字")
            if amount < 0:
                raise HTTPException(400, "金额/体重不能为负")
            channel = (body.get("channel") if body.get("channel") is not None else r["channel"]) or ""
            category = (body.get("category") if body.get("category") is not None else r["category"]) or ""
            note = (body.get("note") if body.get("note") is not None else r["note"]) or ""
            conn.execute(
                "UPDATE ledger_records SET date=?,kind=?,channel=?,category=?,amount=?,note=?,updated_at=? WHERE id=? AND user_id=?",
                (date, kind, channel, category, amount, note, int(time.time() * 1000), rid, uid),
            )
            conn.commit()
        finally:
            conn.close()
    return {"ok": True}

@router.delete("/api/ledger/records/{rid}")
def delete_ledger(rid: str, uid: str = Depends(require_uid)):
    with write_lock:
        conn = get_conn()
        try:
            cur = conn.execute("DELETE FROM ledger_records WHERE id=? AND user_id=?", (rid, uid))
            conn.commit()
            if cur.rowcount == 0:
                return JSONResponse(status_code=404, content={"error": "记录不存在"})
        finally:
            conn.close()
    return {"ok": True}

@router.get("/api/ledger/summary")
def ledger_summary(uid: str = Depends(require_uid), month: str = ""):
    if not month:
        month = time.strftime("%Y-%m")
    mf, mp = _month_filter(month)
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM ledger_records WHERE user_id=? " + mf, (uid,) + mp).fetchall()
    finally:
        conn.close()
    income = expense = 0.0
    channels = {"wechat": 0.0, "alipay": 0.0, "cash": 0.0, "card": 0.0}
    cat_exp = {}
    weights = []
    txn_count = 0
    for r in rows:
        if r["kind"] == "income":
            income += r["amount"]
        elif r["kind"] == "expense":
            expense += r["amount"]
            ch = r["channel"] or "cash"
            if ch in channels:
                channels[ch] += r["amount"]
            cat = r["category"] or "其他"
            cat_exp[cat] = cat_exp.get(cat, 0.0) + r["amount"]
            txn_count += 1
        elif r["kind"] == "weight":
            weights.append({"date": r["date"], "weight": r["amount"]})
    weights.sort(key=lambda x: x["date"])
    w_vals = [w["weight"] for w in weights]
    weight_stat = None
    if w_vals:
        weight_stat = {
            "points": weights,
            "latest": w_vals[-1], "first": w_vals[0],
            "min": min(w_vals), "max": max(w_vals),
            "avg": round(sum(w_vals) / len(w_vals), 2),
            "change": round(w_vals[-1] - w_vals[0], 2),
            "count": len(w_vals),
        }
    cat_list = sorted(
        [{"category": k, "total": round(v, 2)} for k, v in cat_exp.items()],
        key=lambda x: x["total"], reverse=True,
    )
    return {
        "month": month,
        "income": round(income, 2), "expense": round(expense, 2),
        "balance": round(income - expense, 2),
        "channels": {k: round(v, 2) for k, v in channels.items()},
        "categoryExpense": cat_list,
        "weight": weight_stat,
        "txnCount": txn_count,
    }
