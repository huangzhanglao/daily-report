# -*- coding: utf-8 -*-
"""大模型配置 / 对话转发 —— APIRouter。拆分自原 app.py，逻辑/鉴权/返回结构与拆分前完全一致。"""
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

# ---------------- 大模型配置（每个用户可接入自己的 OpenAI 兼容服务） ----------------
def _mask_key(k: str) -> str:
    if not k:
        return ""
    if len(k) <= 4:
        return "****"
    return "****" + k[-4:]

def _llm_row(r, mask: bool = True) -> dict:
    return {
        "id": r["id"], "name": r["name"], "base_url": r["base_url"], "model": r["model"],
        "is_default": bool(r["is_default"]),
        "api_key": _mask_key(r["api_key"]) if mask else r["api_key"],
        "createdAt": r["created_at"], "updatedAt": r["updated_at"],
    }

def _normalize_base_url(u: str) -> str:
    u = (u or "").strip()
    if not u:
        return ""
    if not u.startswith("http://") and not u.startswith("https://"):
        u = "https://" + u
    return u.rstrip("/")

async def _call_llm_test(base_url, api_key, model):
    base_url = _normalize_base_url(base_url)
    if not base_url or not api_key or not model:
        return {"ok": False, "message": "base_url / api_key / model 均必填"}
    url = base_url + "/chat/completions"
    payload = {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1, "temperature": 0}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"}, json=payload)
        if resp.status_code == 200:
            return {"ok": True, "message": "连接成功（模型可用）"}
        try:
            err = resp.json()
        except Exception:
            err = resp.text
        return {"ok": False, "message": "HTTP %d: %s" % (resp.status_code, str(err)[:300])}
    except Exception as e:
        return {"ok": False, "message": "连接失败：" + str(e)[:200]}

@router.get("/api/llm/providers")
def list_llm(uid: str = Depends(require_uid)):
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM llm_providers WHERE user_id=? ORDER BY is_default DESC, updated_at DESC", (uid,)
        ).fetchall()
    finally:
        conn.close()
    return {"providers": [_llm_row(r) for r in rows]}

@router.post("/api/llm/providers")
async def create_llm(body: dict, uid: str = Depends(require_uid)):
    name = (body.get("name") or "").strip()
    base_url = (body.get("base_url") or "").strip()
    api_key = (body.get("api_key") or "").strip()
    model = (body.get("model") or "").strip()
    if not name:
        raise HTTPException(400, "请填写配置名称")
    if not base_url:
        raise HTTPException(400, "请填写 Base URL")
    if not api_key:
        raise HTTPException(400, "请填写 API Key")
    if not model:
        raise HTTPException(400, "请填写模型名称")
    is_default = bool(body.get("is_default"))
    now = int(time.time() * 1000)
    pid = uid_now()
    with write_lock:
        conn = get_conn()
        try:
            if is_default:
                conn.execute("UPDATE llm_providers SET is_default=0 WHERE user_id=?", (uid,))
            conn.execute(
                "INSERT INTO llm_providers (id,user_id,name,base_url,api_key,model,is_default,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (pid, uid, name, base_url, api_key, model, 1 if is_default else 0, now, now),
            )
            conn.commit()
        finally:
            conn.close()
    return {"ok": True, "id": pid}

@router.put("/api/llm/providers/{pid}")
async def update_llm(pid: str, body: dict, uid: str = Depends(require_uid)):
    with write_lock:
        conn = get_conn()
        try:
            r = conn.execute("SELECT * FROM llm_providers WHERE id=? AND user_id=?", (pid, uid)).fetchone()
            if not r:
                raise HTTPException(404, "配置不存在")
            name = (body.get("name") if body.get("name") is not None else r["name"]) or ""
            base_url = (body.get("base_url") if body.get("base_url") is not None else r["base_url"]) or ""
            model = (body.get("model") if body.get("model") is not None else r["model"]) or ""
            api_key = r["api_key"]
            if body.get("api_key") is not None and str(body.get("api_key")).strip():
                api_key = str(body.get("api_key")).strip()
            is_default = bool(body.get("is_default")) if body.get("is_default") is not None else bool(r["is_default"])
            if is_default:
                conn.execute("UPDATE llm_providers SET is_default=0 WHERE user_id=?", (uid,))
            conn.execute(
                "UPDATE llm_providers SET name=?,base_url=?,api_key=?,model=?,is_default=?,updated_at=? WHERE id=? AND user_id=?",
                (name, base_url, api_key, model, 1 if is_default else 0, int(time.time() * 1000), pid, uid),
            )
            conn.commit()
        finally:
            conn.close()
    return {"ok": True}

@router.delete("/api/llm/providers/{pid}")
def delete_llm(pid: str, uid: str = Depends(require_uid)):
    with write_lock:
        conn = get_conn()
        try:
            conn.execute("DELETE FROM llm_providers WHERE id=? AND user_id=?", (pid, uid))
            conn.commit()
        finally:
            conn.close()
    return {"ok": True}

@router.post("/api/llm/test")
async def test_llm(body: dict, uid: str = Depends(require_uid)):
    return await _call_llm_test(body.get("base_url"), body.get("api_key"), body.get("model"))

@router.post("/api/llm/providers/{pid}/test")
async def test_llm_id(pid: str, uid: str = Depends(require_uid)):
    conn = get_conn()
    try:
        r = conn.execute("SELECT * FROM llm_providers WHERE id=? AND user_id=?", (pid, uid)).fetchone()
    finally:
        conn.close()
    if not r:
        raise HTTPException(404, "配置不存在")
    return await _call_llm_test(r["base_url"], r["api_key"], r["model"])

@router.post("/api/llm/chat")
async def llm_chat(body: dict, uid: str = Depends(require_uid)):
    """通用对话转发代理：为后续上架大模型应用（如 AI 记账助手）提供统一入口。
    仅使用服务端存储的密钥，前端不接触明文 key。"""
    pid = body.get("provider_id") or ""
    conn = get_conn()
    try:
        if pid:
            r = conn.execute("SELECT * FROM llm_providers WHERE id=? AND user_id=?", (pid, uid)).fetchone()
        else:
            r = conn.execute("SELECT * FROM llm_providers WHERE user_id=? AND is_default=1 LIMIT 1", (uid,)).fetchone()
            if not r:
                r = conn.execute("SELECT * FROM llm_providers WHERE user_id=? ORDER BY updated_at DESC LIMIT 1", (uid,)).fetchone()
    finally:
        conn.close()
    if not r:
        raise HTTPException(400, "尚未配置任何大模型，请先在「设置 → 大模型配置」中添加")
    base_url = _normalize_base_url(r["base_url"])
    api_key = r["api_key"]
    model = body.get("model") or r["model"]
    if not model:
        raise HTTPException(400, "未指定模型")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(400, "messages 必填且为数组")
    payload = {"model": model, "messages": messages}
    for k in ("temperature", "top_p", "max_tokens", "stream"):
        if k in body and body[k] is not None:
            payload[k] = body[k]
    url = base_url + "/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"}, json=payload)
            data = resp.json()
    except Exception as e:
        raise HTTPException(502, "调用大模型失败：" + str(e)[:200])
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, "大模型返回错误：" + str(data)[:300])
    try:
        content = data["choices"][0]["message"]["content"]
    except Exception:
        content = ""
    return {"ok": True, "content": content, "model": data.get("model", model), "usage": data.get("usage"), "raw": data}
