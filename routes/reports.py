# -*- coding: utf-8 -*-
"""日报 + 博客（report/blog 应用） —— APIRouter。拆分自原 app.py，逻辑/鉴权/返回结构与拆分前完全一致。"""
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

# ---------------- 日报（强制登录 + 按用户隔离） ----------------
@router.get("/api/reports")
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


@router.post("/api/reports")
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


@router.put("/api/reports/{rid}")
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


@router.delete("/api/reports/{rid}")
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
@router.get("/api/blogs")
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


@router.get("/api/blogs/{bid}")
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


@router.post("/api/blogs")
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


@router.put("/api/blogs/{bid}")
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


@router.delete("/api/blogs/{bid}")
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
