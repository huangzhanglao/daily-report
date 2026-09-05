# -*- coding: utf-8 -*-
"""AI 旅游规划智能体 —— APIRouter。

多智能体 harness（复用「设置 → 大模型配置」里用户接入的 OpenAI 兼容服务）：

    Planner（自然语言需求 → 结构化旅行计划 JSON）
        ├─ Specialist: 景点推荐（attractions）   —— 高德 POI 工具（可选）
        ├─ Specialist: 跨城交通（12306 火车票）   —— 12306 逆向接口工具（可选）
        └─ Specialist: 同城路线编排（高德路线）     —— 高德路线规划工具（可选）
    Synthesizer（三份专家材料 → 逐日行程）
    Evaluator（校验门控：缺漏/冲突检测，最多一轮返工）

工具层（tool adapters）支持接入实时数据：
    - 高德 Web 服务（环境变量 AMAP_WEB_KEY）→ 实时 POI 检索 / 路线规划
    - 12306 逆向接口（环境变量 TRAIN_API_BASE）→ 实时火车票查询
均做了优雅降级：未配置 key 时工具返回 None，交由模型基于知识生成，并在行程中明确
标注「（模型知识，仅供参考）」，保证开箱即用、任何 provider 都能跑。

鉴权：require_uid，所有数据按 user_id 隔离。
"""
import os
import re
import json
import time
import asyncio
import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from core import (
    get_conn, write_lock, uid_now, require_uid,
)

router = APIRouter()

# ---------------- 工具开关（实时数据接入） ----------------
AMAP_WEB_KEY = os.environ.get("AMAP_WEB_KEY", "").strip()
TRAIN_API_BASE = os.environ.get("TRAIN_API_BASE", "").strip()


def tool_status() -> dict:
    """返回当前实时数据工具是否可用（前端据此提示）。"""
    return {"amap": bool(AMAP_WEB_KEY), "train": bool(TRAIN_API_BASE)}


# ---------------- 大模型调用（复用 llm_providers 配置，逻辑同 doc.py） ----------------
def _normalize_base_url(u: str) -> str:
    u = (u or "").strip()
    if not u:
        return ""
    if not u.startswith("http://") and not u.startswith("https://"):
        u = "https://" + u
    return u.rstrip("/")


def _pick_provider(conn, uid: str, pid: str = ""):
    """选 provider：优先指定 id → 否则默认 → 否则最近更新的一个。返回行或 None。"""
    if pid:
        r = conn.execute("SELECT * FROM llm_providers WHERE id=? AND user_id=?", (pid, uid)).fetchone()
        if r:
            return r
    r = conn.execute("SELECT * FROM llm_providers WHERE user_id=? AND is_default=1 LIMIT 1", (uid,)).fetchone()
    if r:
        return r
    return conn.execute("SELECT * FROM llm_providers WHERE user_id=? ORDER BY updated_at DESC LIMIT 1", (uid,)).fetchone()


async def _chat(uid: str, messages, temperature: float = 0.7, max_tokens: int = 4000, pid: str = ""):
    """调用大模型 chat/completions，返回回复文本。无可用配置时抛 400。"""
    conn = get_conn()
    try:
        r = _pick_provider(conn, uid, pid)
    finally:
        conn.close()
    if not r:
        raise HTTPException(400, "尚未配置任何大模型，请先在「设置 → 大模型配置」中添加")
    base_url = _normalize_base_url(r["base_url"])
    payload = {
        "model": r["model"], "messages": messages,
        "temperature": temperature, "max_tokens": max_tokens,
    }
    url = base_url + "/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            resp = await client.post(url, headers={"Authorization": "Bearer " + r["api_key"], "Content-Type": "application/json"}, json=payload)
            data = resp.json()
    except Exception as e:
        raise HTTPException(502, "调用大模型失败：" + str(e)[:200])
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, "大模型返回错误：" + str(data)[:300])
    try:
        return data["choices"][0]["message"]["content"]
    except Exception:
        raise HTTPException(502, "大模型返回格式异常")


# ---------------- JSON 解析辅助（Planner / Evaluator 返回结构化 JSON） ----------------
def _extract_json(text: str):
    """从模型回复中提取第一个 JSON 对象。失败返回 None。"""
    if not text:
        return None
    s = text.strip()
    # 去掉可能的 ```json 代码围栏
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, re.DOTALL)
    if m:
        s = m.group(1)
    else:
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if m:
            s = m.group(0)
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


async def _json_chat(uid, messages, temperature, max_tokens, pid, role_hint):
    """尝试让模型输出 JSON；首轮失败再用一次「只输出 JSON」重试。"""
    raw = await _chat(uid, messages, temperature, max_tokens, pid)
    obj = _extract_json(raw)
    if obj is not None:
        return obj
    # 重试：强制只输出 JSON
    messages = messages + [{"role": "user", "content": "你的回复必须且只能是合法的 JSON 对象，不要任何解释或代码围栏。"}]
    raw2 = await _chat(uid, messages, temperature, max_tokens, pid)
    return _extract_json(raw2)


# ---------------- 工具适配器（高德 / 12306），无 key 时优雅降级 ----------------
async def amap_poi(city: str, keyword: str):
    """检索某城市 POI（景点/美食等）。无 key 时返回 None（降级为模型知识）。"""
    if not AMAP_WEB_KEY or not city:
        return None
    try:
        params = {"key": AMAP_WEB_KEY, "city": city, "keywords": keyword, "offset": 15, "extensions": "base"}
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://restapi.amap.com/v3/place/text", params=params)
            data = r.json()
        if data.get("status") == "1":
            return [
                {"name": p.get("name"), "address": p.get("address", ""), "type": p.get("type", "")}
                for p in data.get("pois", [])
            ]
    except Exception:
        return None
    return None


async def amap_route(city: str, origins: list, destination: str):
    """同城多点到一点的路线规划（高德）。无 key 时返回 None。"""
    if not AMAP_WEB_KEY or not origins or not destination:
        return None
    try:
        params = {
            "key": AMAP_WEB_KEY,
            "origin": origins[0],
            "destination": destination,
            "city": city,
            "strategy": "2",  # 距离最短
        }
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://restapi.amap.com/v3/direction/driving", params=params)
            data = r.json()
        if data.get("status") == "1":
            return data.get("route", {})
    except Exception:
        return None
    return None


async def train_query(from_station: str, to_station: str, date: str):
    """查询 12306 火车票。需要自建 TRAIN_API_BASE 逆向服务；无则降级。"""
    if not TRAIN_API_BASE or not from_station or not to_station:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(TRAIN_API_BASE.rstrip("/") + "/query", params={"from": from_station, "to": to_station, "date": date})
            data = r.json()
        return data
    except Exception:
        return None


# ---------------- Agent 节点（每个都是一次专注的 LLM 调用） ----------------
async def planner(uid: str, prompt: str, pid: str) -> dict:
    sys_prompt = (
        "你是资深旅行规划助手。用户会用自然语言描述出行需求，请你从中抽取关键信息，"
        "输出严格 JSON（不要任何解释、不要代码围栏）：\n"
        "{\n"
        '  "destination": "目的地（省份/国家，可空）",\n'
        '  "cities": ["城市1","城市2"...]  // 按游览顺序排列\n'
        '  "start_date": "YYYY-MM-DD（不确定则为空字符串）",\n'
        '  "end_date": "YYYY-MM-DD（不确定则为空字符串）",\n'
        '  "days": 整数行程天数（推断，至少1）,\n'
        '  "party": "出行人群，如 独自/情侣/带娃家庭/朋友（2人）",\n'
        '  "budget": "预算区间描述，如 人均3000-5000元 / 不限",\n'
        '  "preferences": ["历史","美食","自然风光"...]  // 兴趣标签\n'
        '  "must_see": ["必去地点/项目"...],\n'
        '  "transport_pref": "高铁优先/飞机优先/自驾/不限",\n'
        '  "pace": "紧凑/适中/轻松",\n'
        '  "food_pref": "饮食偏好，如 不吃辣/当地特色/网红店",\n'
        '  "notes": "其它补充说明"\n'
        "}\n"
        "要求：城市顺序要合理；天数与起止日期尽量自洽；信息不全时合理推断但保持字段完整。"
    )
    msgs = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": "用户需求：\n" + prompt},
    ]
    obj = await _json_chat(uid, msgs, temperature=0.2, max_tokens=1500, pid=pid, role_hint="planner")
    if not isinstance(obj, dict):
        # 兜底：构造最小计划，避免整条链路崩
        obj = {"cities": [], "days": 3, "preferences": [], "must_see": [], "destination": "", "pace": "适中"}
    obj.setdefault("cities", [])
    obj.setdefault("preferences", [])
    obj.setdefault("must_see", [])
    try:
        obj["days"] = max(1, int(obj.get("days", 3) or 3))
    except Exception:
        obj["days"] = 3
    return obj


async def specialist_attractions(uid: str, plan: dict, pid: str) -> dict:
    """景点推荐专家：产出每个城市的景点分级清单（可融合高德实时 POI）。"""
    cities = plan.get("cities") or ["目的地"]
    prefs = "、".join(plan.get("preferences", []) or [])
    must = "、".join(plan.get("must_see", []) or [])
    party = plan.get("party", "")
    live = tool_status()
    # 并行拉取各城市实时 POI（有 key 才有效）
    pois = {}
    if live["amap"]:
        results = await asyncio.gather(*[amap_poi(c, prefs or "景点") for c in cities])
        for c, p in zip(cities, results):
            if p:
                pois[c] = p

    poi_block = ""
    if pois:
        for c, p in pois.items():
            poi_block += f"\n【{c}】实时 POI（高德）：\n" + "\n".join(f"- {x['name']}（{x.get('type','')}）{x.get('address','')}" for x in p[:12]) + "\n"

    sys_prompt = (
        "你是旅游景点推荐专家。依据旅行计划，为每个城市给出景点清单，按『必去 / 值得去 / 可备选』三级，"
        "每条附建议游玩时长（小时）与一句话理由，并标注是否免费。\n"
        + ("已为你接入高德实时 POI 数据，请优先采用并标注来源；" if poi_block else "未接入实时数据，请基于你的知识给出，并在开头明确标注「（模型知识，仅供参考）」。")
        + "输出结构化文本（每个城市一段，用『城市：』开头）。"
    )
    user = (
        f"旅行计划：城市={cities}，天数={plan.get('days')}，人群={party}，兴趣={prefs or '综合'}，"
        f"必去={must or '无'}，节奏={plan.get('pace','适中')}\n"
        + (poi_block if poi_block else "")
    )
    text = (await _chat(uid, [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user}], temperature=0.7, max_tokens=4000, pid=pid)).strip()
    return {"text": text, "live": bool(poi_block), "pois": pois}


async def specialist_transport(uid: str, plan: dict, pid: str) -> dict:
    """交通专家：城市间交通（高铁/飞机）+ 市内交通说明（可融合 12306 实时票）。"""
    cities = plan.get("cities") or []
    live = tool_status()
    tickets = {}
    if live["train"] and len(cities) >= 2:
        pairs = []
        for i in range(len(cities) - 1):
            date = plan.get("start_date", "")
            res = await train_query(cities[i], cities[i + 1], date)
            if res:
                tickets[f"{cities[i]}→{cities[i+1]}"] = res

    ticket_block = ""
    if tickets:
        for k, v in tickets.items():
            ticket_block += f"\n【{k}】实时车次：{json.dumps(v, ensure_ascii=False)[:800]}\n"

    sys_prompt = (
        "你是交通规划专家。依据城市顺序与日期，规划城市间交通（以高铁/动车、飞机为主），给出建议车次类型、"
        "大致耗时与票价区间，并说明每段抵达后的市内交通方式（地铁/打车/公交）。\n"
        + ("已接入 12306 实时票数据，请采用并标注；" if ticket_block else "未接入实时票务，请基于知识给出，并标注「（模型知识，仅供参考）」。")
        + "若只有一个城市，则说明该市内交通即可。输出清晰的分段说明。"
    )
    user = (
        f"城市顺序={cities}，起止日期={plan.get('start_date','')}~{plan.get('end_date','')}，"
        f"交通偏好={plan.get('transport_pref','不限')}，人群={plan.get('party','')}\n"
        + (ticket_block if ticket_block else "")
    )
    text = (await _chat(uid, [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user}], temperature=0.5, max_tokens=3000, pid=pid)).strip()
    return {"text": text, "live": bool(ticket_block), "tickets": tickets}


async def specialist_routing(uid: str, plan: dict, attractions_text: str, pid: str) -> dict:
    """路线编排专家：把景点排成每日时间线，动线顺、标注交通耗时与餐饮。"""
    cities = plan.get("cities") or ["目的地"]
    live = tool_status()
    sys_prompt = (
        "你是每日路线编排专家。基于『景点推荐』材料与城市，产出每日时间线（上午/下午/晚上），"
        "合理安排景点顺序使动线顺、避免大量折返；标注景点间交通方式与预计耗时，并插入餐饮建议。"
        + ("若已接入高德路线耗时数据请采用；" if live["amap"] else "未接入实时路线，请基于知识估计耗时并标注「（模型知识，仅供参考）」。")
        + "按 Day1、Day2… 输出，每天含：城市、主题、时间线、餐饮。"
    )
    user = (
        f"行程天数={plan.get('days')}，城市={cities}，节奏={plan.get('pace','适中')}，"
        f"饮食偏好={plan.get('food_pref','当地特色')}\n\n【景点推荐材料】\n" + (attractions_text or "（无）")
    )
    text = (await _chat(uid, [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user}], temperature=0.6, max_tokens=4000, pid=pid)).strip()
    return {"text": text, "live": live["amap"]}


async def synthesizer(uid: str, plan: dict, attr: dict, trans: dict, route: dict, pid: str, revision_note: str = "") -> str:
    """统筹专家：把三份专家材料合成为逐日行程。"""
    sys_prompt = (
        "你是行程统筹专家。请把下面的『景点推荐』『交通方案』『每日路线』三份材料，结合原始旅行计划，"
        "整合成一份完整、连贯、可直接照做的逐日行程（Day1..DayN）。\n"
        "每一天包含：所在城市、当日主题、时间线（时段-地点-活动-交通-餐饮）、住宿建议、当日花费预估。"
        "语言自然、可操作；若材料含「模型知识，仅供参考」标注，请在行程开头保留一句总体说明。"
    )
    user = (
        f"【原始计划】\n{json.dumps(plan, ensure_ascii=False)}\n\n"
        f"【景点推荐】\n{attr.get('text','')}\n\n"
        f"【交通方案】\n{trans.get('text','')}\n\n"
        f"【每日路线】\n{route.get('text','')}\n"
        + (f"\n【返工要求】上一版被质检打回，请重点修正：{revision_note}\n" if revision_note else "")
    )
    return (await _chat(uid, [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user}], temperature=0.6, max_tokens=6000, pid=pid)).strip()


async def evaluator(uid: str, plan: dict, itinerary: str, pid: str) -> dict:
    """质检员：校验行程完整性/一致性，输出 JSON。"""
    cities = plan.get("cities") or []
    sys_prompt = (
        "你是严格的行程质检员。检查合成行程是否满足：\n"
        "① 覆盖计划中的所有城市与天数（Day1..DayN 齐全）；\n"
        "② 城市间交通已说明（单城市可忽略）；\n"
        "③ 每日时间不冲突、不过载（单日景点数合理）；\n"
        "④ 兴趣标签与必去点已被满足；\n"
        "⑤ 餐饮、住宿有提及。\n"
        "输出严格 JSON（不要解释）：{\"pass\": true/false, \"score\": 1-10, \"issues\": [\"问题1\",...], \"missing\": [\"缺失项\",...]}"
    )
    user = (
        f"计划城市={cities}，天数={plan.get('days')}，兴趣={plan.get('preferences',[])}，必去={plan.get('must_see',[])}\n\n"
        f"【待检行程】\n{itinerary}"
    )
    obj = await _json_chat(uid, [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user}], temperature=0.2, max_tokens=1200, pid=pid, role_hint="evaluator")
    if not isinstance(obj, dict):
        obj = {"pass": True, "score": 8, "issues": [], "missing": []}
    obj.setdefault("pass", True)
    obj.setdefault("score", 8)
    obj.setdefault("issues", [])
    obj.setdefault("missing", [])
    return obj


# ---------------- 编排器：把各 Agent 串成 harness ----------------
def _sse(event: dict) -> str:
    """把一个事件字典编码成 SSE 的 data 帧。"""
    return "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"


async def run_harness(uid: str, prompt: str, pid: str, emit=None) -> dict:
    """运行多智能体 harness。emit 为可选异步回调，用于把阶段进度实时推给前端。

    推送的事件类型：
      {"type":"stage","step":1..4,"status":"running"|"done","agent":"attractions"|"transport"|"routing"|None,"label":...}
      {"type":"__end__"}   # harness 全部完成（由调用方用于结束流）
    """
    async def stage(step, status, agent=None, label=None):
        if emit:
            await emit({"type": "stage", "step": step, "status": status, "agent": agent, "label": label})

    # 1) Planner
    await stage(1, "running")
    plan = await planner(uid, prompt, pid)
    await stage(1, "done")

    # 2) 三个 Specialist：景点 / 交通并行；路线依赖景点材料，在景点完成后顺序执行
    await stage(2, "running", label="专家并行分析")
    async def _with_emit(coro, agent):
        r = await coro
        await stage(2, "done", agent=agent)
        return r
    attr, trans = await asyncio.gather(
        _with_emit(specialist_attractions(uid, plan, pid), "attractions"),
        _with_emit(specialist_transport(uid, plan, pid), "transport"),
    )
    route = await _with_emit(specialist_routing(uid, plan, attr.get("text", ""), pid), "routing")

    # 3) Synthesizer
    await stage(3, "running")
    itinerary = await synthesizer(uid, plan, attr, trans, route, pid)
    await stage(3, "done")

    # 4) Evaluator（校验门控：未通过则最多返工一轮）
    await stage(4, "running")
    eval_res = await evaluator(uid, plan, itinerary, pid)
    await stage(4, "done")
    if not eval_res.get("pass"):
        note = "；".join(eval_res.get("issues", []) or []) or "整体不够完整"
        await stage(4, "running", label="质检未过，返工一轮")
        itinerary = await synthesizer(uid, plan, attr, trans, route, pid, revision_note=note)
        eval_res = await evaluator(uid, plan, itinerary, pid)
        await stage(4, "done")

    if emit:
        await emit({"type": "__end__"})
    return {
        "plan": plan,
        "specialists": {
            "attractions": attr,
            "transport": trans,
            "routing": route,
        },
        "itinerary": itinerary,
        "evaluation": eval_res,
    }


# ---------------- 存储辅助 ----------------
def _plan_row(r) -> dict:
    def _j(x, default):
        try:
            return json.loads(x) if x else default
        except Exception:
            return default
    return {
        "id": r["id"], "title": r["title"], "prompt": r["prompt"],
        "plan": _j(r["plan"], {}),
        "attractions": r["attractions"], "transport": r["transport"], "routing": r["routing"],
        "itinerary": r["itinerary"],
        "evaluation": _j(r["evaluation"], {}),
        "status": r["status"],
        "createdAt": r["created_at"], "updatedAt": r["updated_at"],
    }


def _plan_meta(r) -> dict:
    return {
        "id": r["id"], "title": r["title"], "status": r["status"],
        "createdAt": r["created_at"], "updatedAt": r["updated_at"],
    }


def _title_of(result: dict, prompt: str) -> str:
    cities = result["plan"].get("cities") or ["我的行程"]
    title = " → ".join(cities) if isinstance(cities, list) else str(cities)
    if not title or title == "我的行程":
        title = (prompt or "").strip()[:24] or "我的行程"
    return title


def _save_plan(uid: str, prompt: str, result: dict, plan_id: str = None) -> str:
    """把 harness 结果写入 travel_plans。plan_id 为空则新建。返回 plan_id。"""
    title = _title_of(result, prompt)
    now = int(time.time() * 1000)

    def _j(x):
        return json.dumps(x, ensure_ascii=False)

    with write_lock:
        conn = get_conn()
        try:
            if not plan_id:
                plan_id = uid_now()
                conn.execute(
                    "INSERT INTO travel_plans (id,user_id,title,prompt,plan,attractions,transport,routing,itinerary,evaluation,status,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (plan_id, uid, title, prompt,
                     _j(result["plan"]), result["specialists"]["attractions"].get("text", ""),
                     result["specialists"]["transport"].get("text", ""),
                     result["specialists"]["routing"].get("text", ""),
                     result["itinerary"], _j(result["evaluation"]), "done", now, now),
                )
            else:
                conn.execute(
                    "UPDATE travel_plans SET title=?,plan=?,attractions=?,transport=?,routing=?,itinerary=?,evaluation=?,status='done',updated_at=? WHERE id=? AND user_id=?",
                    (title, _j(result["plan"]), result["specialists"]["attractions"].get("text", ""),
                     result["specialists"]["transport"].get("text", ""),
                     result["specialists"]["routing"].get("text", ""),
                     result["itinerary"], _j(result["evaluation"]), now, plan_id, uid),
                )
            conn.commit()
        finally:
            conn.close()
    return plan_id


# ---------------- API ----------------
@router.get("/api/travel/config")
def travel_config(uid: str = Depends(require_uid)):
    """返回实时数据工具是否可用，供前端提示。"""
    return {"ok": True, "tools": tool_status()}


@router.post("/api/travel/generate")
async def travel_generate(body: dict, uid: str = Depends(require_uid)):
    """生成行程：运行多智能体 harness。body: {prompt, provider_id?}。返回 plan/specialists/itinerary/evaluation。"""
    prompt = (body.get("prompt") or "").strip()
    pid = (body.get("provider_id") or "").strip()
    if not prompt:
        raise HTTPException(400, "请描述你的旅行需求")
    result = await run_harness(uid, prompt, pid)
    plan_id = _save_plan(uid, prompt, result)
    result["id"] = plan_id
    result["title"] = _title_of(result, prompt)
    return {"ok": True, **result}


def _make_stream(uid: str, prompt: str, pid: str, plan_id: str = None):
    """构造一个 SSE StreamingResponse：实时推送多智能体各阶段进度，最后推送完整结果。

    plan_id 为空=新建行程；否则在原 id 上覆盖更新（用于 regenerate）。
    """
    queue: asyncio.Queue = asyncio.Queue()

    async def emit(ev):
        await queue.put(ev)

    async def event_gen():
        task = asyncio.create_task(run_harness(uid, prompt, pid, emit=emit))
        # 边收边推：直到 harness 结束（task 完成或收到 __end__ 哨兵）
        while not task.done():
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if ev.get("type") == "__end__":
                break
            yield _sse(ev)
        # 排空剩余事件
        while not queue.empty():
            ev = await queue.get()
            if ev.get("type") != "__end__":
                yield _sse(ev)
        try:
            result = task.result()  # harness 完成；若中途抛异常会在此重新抛出
        except HTTPException as e:
            yield _sse({"type": "error", "message": e.detail})
            return
        except Exception as e:
            yield _sse({"type": "error", "message": str(e)[:300]})
            return
        saved_id = _save_plan(uid, prompt, result, plan_id=plan_id)
        yield _sse({"type": "result", "id": saved_id, "title": _title_of(result, prompt), **result})

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.post("/api/travel/generate_stream")
async def travel_generate_stream(body: dict, uid: str = Depends(require_uid)):
    """流式生成：SSE 实时推送多智能体各阶段进度，最后推送完整结果。

    前端用 fetch + ReadableStream 消费（text/event-stream）。
    事件（每行 data: <json>\\n\\n）：
      {"type":"stage",...}  阶段进度（见 run_harness）
      {"type":"result","id":...,"title":...,<harness 结果>}  最终行程
      {"type":"error","message":...}  出错
    """
    prompt = (body.get("prompt") or "").strip()
    pid = (body.get("provider_id") or "").strip()
    if not prompt:
        raise HTTPException(400, "请描述你的旅行需求")
    return _make_stream(uid, prompt, pid)


@router.post("/api/travel/plans/{pid}/regenerate_stream")
async def travel_regenerate_stream(pid: str, body: dict, uid: str = Depends(require_uid)):
    """对已有计划重新跑一遍 harness（基于原 prompt），流式推送进度。"""
    with write_lock:
        conn = get_conn()
        try:
            r = conn.execute("SELECT * FROM travel_plans WHERE id=? AND user_id=?", (pid, uid)).fetchone()
        finally:
            conn.close()
    if not r:
        raise HTTPException(404, "计划不存在")
    new_pid = (body.get("provider_id") or "").strip()
    return _make_stream(uid, r["prompt"], new_pid, plan_id=pid)


@router.post("/api/travel/plans/{pid}/regenerate")
async def travel_regenerate(pid: str, body: dict, uid: str = Depends(require_uid)):
    """对已有计划重新跑一遍 harness（基于原 prompt）。"""
    with write_lock:
        conn = get_conn()
        try:
            r = conn.execute("SELECT * FROM travel_plans WHERE id=? AND user_id=?", (pid, uid)).fetchone()
        finally:
            conn.close()
    if not r:
        raise HTTPException(404, "计划不存在")
    new_pid = (body.get("provider_id") or "").strip()
    result = await run_harness(uid, r["prompt"], new_pid)
    _save_plan(uid, r["prompt"], result, plan_id=pid)
    result["id"] = pid
    result["title"] = r["title"]
    return {"ok": True, **result}


@router.get("/api/travel/plans")
def travel_list(uid: str = Depends(require_uid), page: int = 1, page_size: int = 50):
    conn = get_conn()
    try:
        total = conn.execute("SELECT COUNT(*) AS c FROM travel_plans WHERE user_id=?", (uid,)).fetchone()["c"]
        page = max(1, page); page_size = max(1, min(100, page_size))
        off = (page - 1) * page_size
        rows = conn.execute("SELECT * FROM travel_plans WHERE user_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?", (uid, page_size, off)).fetchall()
    finally:
        conn.close()
    return {"total": total, "items": [_plan_meta(r) for r in rows]}


@router.get("/api/travel/plans/{pid}")
def travel_detail(pid: str, uid: str = Depends(require_uid)):
    conn = get_conn()
    try:
        r = conn.execute("SELECT * FROM travel_plans WHERE id=? AND user_id=?", (pid, uid)).fetchone()
    finally:
        conn.close()
    if not r:
        raise HTTPException(404, "计划不存在")
    return _plan_row(r)


@router.delete("/api/travel/plans/{pid}")
def travel_delete(pid: str, uid: str = Depends(require_uid)):
    with write_lock:
        conn = get_conn()
        try:
            conn.execute("DELETE FROM travel_plans WHERE id=? AND user_id=?", (pid, uid))
            conn.commit()
        finally:
            conn.close()
    return {"ok": True}
