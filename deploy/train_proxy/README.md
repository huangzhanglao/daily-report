# 12306 实时查票代理（train_proxy）

让「旅游智能规划」真正拿到 12306 实时车次 / 余票。

## 为什么需要它

12306 对纯服务端请求（Python / curl）有 WAF 拦截（实测返回「铁路客户服务中心」防火墙页），
所以 `daily-report` 内置直连在绝大多数服务器环境会被降级。本代理用**真实无头 Chromium**
访问 12306：拿到浏览器才会设置的 `RAIL` cookie + 真实 TLS/JA3 指纹，从而绕过 WAF，
再用页面内 `fetch` 拉取 `leftTicket/queryO` 的真实 JSON。

> ⚠️ **关键约束**：12306 是按**出口 IP** 封的。即便用真实浏览器，只要从被封锁的 IP（如本开发机）
> 访问，依然会被拦。因此本代理**必须部署到一个不被 12306 封锁的节点**（国内服务器 / 住宅出口 /
> 你自有的、IP 干净的机器）。同一台被封机器上"本机代理"和"本机直连"效果一样——都不行。

## 契约

```
GET /query?from=北京&to=上海&date=2026-09-17[&token=xxx]
-> {"date","from","to","trains":[{"train","from","to","depart","arrive","duration","seats":[...]}]}
GET /health
-> {"ok": true, "stations": N, "playwright": true}
```

`daily-report` 的 `routes/travel.py` 已按此消费：填写 `TRAIN_API_BASE` 后即走本代理。

## 部署方式

### 方式 A：Docker（推荐，最省事）

```bash
cd deploy/train_proxy
# 公网部署务必设置 Token（与下方 daily-report 的 TRAIN_API_TOKEN 一致）
echo "PROXY_TOKEN=你的强Token" > .env
docker compose up -d --build
# 验证
curl http://<节点IP>:8799/health
```

镜像基于官方 `mcr.microsoft.com/playwright/python`，已含 Chromium，**无需再下载浏览器**，
构建快、体积小。

### 方式 B：原生运行（无 Docker）

```bash
bash deploy/train_proxy/start.sh        # Linux / macOS / WSL
# 或 Windows： 直接运行 deploy\train_proxy\run_train_proxy.bat
# 公网部署前先 export PROXY_TOKEN=你的强Token
```

## 安全（公网部署必看）

- 代理本身**不设鉴权也可运行**，但公网暴露会允许任何人借你的节点刷 12306。
- 设置环境变量 `PROXY_TOKEN=你的强Token` 后，代理要求每次 `/query` 带 token：
  - URL 参数：`/query?...&token=你的强Token`
  - 或请求头：`Authorization: Bearer 你的强Token`
- 在 `daily-report` 的 **设置 → 数据源配置** 页填写同一个值到 `TRAIN_API_TOKEN`，两端必须一致。

## 接入 daily-report

1. 代理在某节点跑起来（例如 `http://203.0.113.10:8799`）。
2. 进入 `daily-report` **设置 → 数据源配置**（管理员）：
   - `12306 代理地址` 填 `http://203.0.113.10:8799`
   - （若代理设了 PROXY_TOKEN）`12306 代理 Token` 填同一值
3. 保存后**立即生效，无需重启**。旅游智能体生成行程时即直出实时车次/余票。

## 排错

| 现象 | 原因 / 处理 |
| --- | --- |
| `/query` 返回 `{"error":"waf_or_empty"}` | 出口 IP 被 12306 封锁 → 换一个不被封的节点部署 |
| `/query` 返回 `401 unauthorized` | 代理设了 PROXY_TOKEN 但调用方没带 / 不一致 → 核对两端 token |
| `/health` 中 `playwright:false` | 未装 playwright → `pip install playwright && playwright install chromium` |
| 启动报 Chromium 缺失库 | 用 Docker 镜像；或原生运行按 Playwright 提示装系统依赖 |
| `stations:0` | 无法拉取 station_name.js（网络/防火墙）→ 检查节点出网 |
