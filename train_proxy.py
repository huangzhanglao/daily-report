# -*- coding: utf-8 -*-
"""12306 实时查票代理（让旅游智能体拿到真实车次/余票）。

为什么需要它：
    12306 对纯服务端（Python/curl）请求有 WAF 拦截（实测返回「铁路客户服务中心」防火墙页），
    所以 daily-report 内置直连在多数服务器环境会降级。本代理用真实无头 Chromium 访问 12306：
    拿到浏览器才会设置的 RAIL cookie + 真实 TLS 指纹（JA3），从而绕过 WAF，再用 page 内 fetch
    拉取 leftTicket/queryO 的真实 JSON。

契约（daily-report 的 routes/travel.py 已按此消费）：
    GET /query?from=北京&to=上海&date=2026-09-17
    -> {"date","from","to","trains":[{"train","from","to","depart","arrive","duration","seats":[...]}]}

运行：
    python train_proxy.py        # 监听 0.0.0.0:8799
然后在 daily-report 设置 → 数据源配置 填写 TRAIN_API_BASE=http://<本机IP>:8799
（与 daily-report 同机则填 http://127.0.0.1:8799）
"""
import asyncio
import json
import re
import ssl
import urllib.request
from datetime import datetime, timedelta
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
import uvicorn

try:
    from playwright.async_api import async_playwright
except Exception as e:  # noqa
    async_playwright = None
    _PW_IMPORT_ERR = e

app = FastAPI(title="12306 train proxy")

STATION_URL = "https://kyfw.12306.cn/otn/resources/js/framework/station_name.js"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 优先复用本机已安装的 Chrome（无需下载 Playwright 自带的 Chromium）
import os as _os
for _cp in (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
):
    if _os.path.exists(_cp):
        CHROME_PATH = _cp
        break
else:
    CHROME_PATH = None
SEAT_MAP = {21: "商务座", 22: "特等座", 23: "一等包座", 24: "一等座", 25: "二等包座",
            26: "二等座", 27: "高级软卧", 28: "软卧", 29: "动卧", 30: "硬卧",
            31: "软座", 32: "硬座", 33: "无座", 34: "其它"}

_STATION_CACHE: dict = {}
_BROWSER = None
_CONTEXT = None
_PW = None
_LOCK = asyncio.Lock()
_RESULT_CACHE: dict = {}
_RESULT_TTL = 120  # 秒


async def ensure_browser():
    global _PW, _BROWSER, _CONTEXT
    if _BROWSER is not None:
        return
    if async_playwright is None:
        raise RuntimeError(f"playwright 未安装: {_PW_IMPORT_ERR}")
    _PW = await async_playwright().start()
    launch_kwargs = dict(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-bot-management"],
    )
    if CHROME_PATH:
        launch_kwargs["executable_path"] = CHROME_PATH
    _BROWSER = await _PW.chromium.launch(**launch_kwargs)
    _CONTEXT = await _BROWSER.new_context(user_agent=UA, locale="zh-CN")
    # 预热：访问 leftTicket/init 让 12306 的 JS 种下 RAIL 浏览器 cookie
    try:
        pg = await _CONTEXT.new_page()
        await pg.goto("https://kyfw.12306.cn/otn/leftTicket/init",
                      wait_until="domcontentloaded", timeout=25000)
        await pg.close()
    except Exception:
        pass


async def load_stations():
    if _STATION_CACHE:
        return _STATION_CACHE
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(STATION_URL, timeout=15, context=ctx) as r:
            txt = r.read().decode("utf-8-sig", "ignore")
        m = re.search(r"station_names\s*=\s*'(.+?)';", txt, re.S)
        body = m.group(1) if m else txt
        for part in body.split("@"):
            f = part.split("|")
            if len(f) >= 3 and f[1] and f[2] and f[1] not in _STATION_CACHE:
                _STATION_CACHE[f[1]] = f[2]
    except Exception:
        pass
    return _STATION_CACHE


def code_of(name: str):
    s = _STATION_CACHE
    if name in s:
        return s[name]
    for k, v in s.items():
        if k.startswith(name) or name.startswith(k):
            return v
    return None


def parse_12306(d, date, frm, to):
    out = []
    data = d.get("data") if isinstance(d, dict) else None
    res = data.get("result") if isinstance(data, dict) else None
    if not res:
        return out
    for row in res[:30]:
        f = row.split("|")
        if len(f) < 35:
            continue
        seats = []
        for i, label in SEAT_MAP.items():
            val = f[i] if i < len(f) else ""
            if val and val not in ("", "--"):
                seats.append(f"{label}{val}")
        out.append({
            "train": f[3], "from": f[6], "to": f[7],
            "depart": f[8], "arrive": f[9], "duration": f[10],
            "seats": seats[:8],
        })
    return out


_FETCH_JS = """
async (u) => {
  const r = await fetch(u, {headers: {
    'Accept': '*/*',
    'Referer': 'https://kyfw.12306.cn/otn/leftTicket/init',
    'X-Requested-With': 'XMLHttpRequest'
  }});
  const t = await r.text();
  try { return JSON.parse(t); } catch(e) { return {__raw__: t.slice(0, 240)}; }
}
"""


async def query_one(fc: str, tc: str, date: str):
    async with _LOCK:
        await ensure_browser()
        pg = await _CONTEXT.new_page()
        try:
            base = ("https://kyfw.12306.cn/otn/leftTicket/queryO"
                    "?leftTicketDTO.train_date=%s"
                    "&leftTicketDTO.from_station=%s"
                    "&leftTicketDTO.to_station=%s"
                    "&leftTicketDTO.distance_kilometers=0&purpose_codes=ADULT" % (date, fc, tc))
            d = await pg.evaluate(_FETCH_JS, base)
            if isinstance(d, dict) and d.get("c_url") and not d.get("status"):
                url2 = "https://kyfw.12306.cn/otn/" + d["c_url"] + "?" + base.split("?", 1)[1]
                d = await pg.evaluate(_FETCH_JS, url2)
            if isinstance(d, dict) and d.get("status") is True:
                return d
            return None
        finally:
            await pg.close()


@app.get("/query")
async def query(from_: str = Query(..., alias="from"),
                to: str = Query(..., alias="to"),
                date: str = Query(...)):
    await load_stations()
    fc, tc = code_of(from_), code_of(to)
    if not fc or not tc:
        return JSONResponse({"date": date, "from": from_, "to": to,
                             "trains": [], "error": "station_not_found"})
    key = f"{fc}|{tc}|{date}"
    now = datetime.now().timestamp()
    cached = _RESULT_CACHE.get(key)
    if cached and now - cached[0] < _RESULT_TTL:
        return cached[1]
    try:
        d = await query_one(fc, tc, date)
    except Exception as e:
        return JSONResponse({"date": date, "from": from_, "to": to,
                             "trains": [], "error": str(e)[:160]})
    if not d:
        return JSONResponse({"date": date, "from": from_, "to": to,
                             "trains": [], "error": "waf_or_empty"})
    trains = parse_12306(d, date, from_, to)
    result = {"date": date, "from": from_, "to": to, "trains": trains}
    _RESULT_CACHE[key] = (now, result)
    return result


@app.get("/health")
async def health():
    return {"ok": True, "stations": len(_STATION_CACHE),
            "playwright": async_playwright is not None}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8799)
