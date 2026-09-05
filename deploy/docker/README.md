# Docker Compose 部署指南（日报工作台）

用 Docker 一键跑起 FastAPI 应用 + Redis(共享限流) + Caddy(反代/自动 HTTPS)。
应用本体继续沿用重构后的多文件结构(`app.py` + `core.py` + `routes/`),打包进镜像。

> 需要在**装有 Docker 的机器**上执行(本项目开发机未装 Docker,文件已备好,拷到目标机即可跑)。

---

## 1. 目录/文件说明

| 文件 | 作用 |
|---|---|
| `Dockerfile`(项目根) | 构建 FastAPI 应用镜像,非 root 运行,`public/` 静态资源打进镜像 |
| `.dockerignore`(项目根) | 构建上下文排除清单,`data/`(含 `.secret`)绝不打进镜像 |
| `docker-compose.yml`(项目根) | 编排 `app` / `redis` / `train_proxy` / `caddy` 四个服务 + 4 个命名卷 |
| `deploy/docker/Caddyfile` | 容器版反代配置,域名用 `{$APP_DOMAIN}` 占位;同时把 `/train` 反代到 `train_proxy:8799` |
| `deploy/train_proxy/Dockerfile` | 12306 代理镜像(基于官方 Playwright 镜像,已含 Chromium),由 compose 的 `train_proxy` 服务引用 |

## 2. 快速开始

```bash
# 本地测试（Caddy 用内部 CA 为 localhost 签证书，浏览器提示"不安全"属正常）
APP_DOMAIN=localhost docker compose up -d --build
# 访问：https://localhost

# 生产（换成你的真实域名，Caddy 自动 ACME 申请/续期证书）
APP_DOMAIN=your.domain.com docker compose up -d --build
# 访问：https://your.domain.com
```

其他常用命令:

```bash
docker compose ps                 # 看三服务状态
docker compose logs -f app        # 盯 app 日志
docker compose logs -f caddy      # 盯 caddy 日志(证书申请/续期)
docker compose down               # 停止并移除容器(保留数据卷)
docker compose down -v            # 停止并删除数据卷(⚠️ 会清空 app.db)
docker compose up -d --build      # 改完代码后重建镜像再起
```

## 3. 数据持久化与首次导入现有库

- 数据库 `app.db` 与签名密钥 `.secret` 存在 **named volume `app-data`**,挂到容器 `/app/data`。
- `.secret` 随卷持久化 → **重启/重建容器后所有用户 token 仍有效**(否则会全员掉登录)。

### 首次部署时,想把本地已有的 `data/` 导进新卷

```bash
# 1. 先把服务拉起来建好卷(或仅建卷): docker compose up -d redis
# 2. 找到卷名: docker volume ls | grep daily-report
#    一般是 daily-report_app-data
# 3. 用一次性容器把本地 data 拷进卷
docker run --rm -v daily-report_app-data:/data -v "$(pwd)/data":/src alpine \
  sh -c "cp -r /src/. /data/ && chmod -R 777 /data"
# 4. 再正常 up 即可
docker compose up -d
```

> 新环境若没有现成 data,应用启动会自动建 `app.db` 并写入种子数据,无需手动初始化。

## 4. 端口覆盖(宿主机 80/443 已被占用时)

编辑 `docker-compose.yml` 中 `caddy.ports`,把宿主端口改掉即可:

```yaml
ports:
  - "8080:80"    # HTTP
  - "8443:443"   # HTTPS
```

改后访问 `https://localhost:8443`(本地)或 `https://your.domain.com:8443`(生产,需配好 DNS/防火墙放行)。

## 5. Redis 限流说明

- compose 已给 app 设 `REDIS_URL=redis://redis:6379/0`,多实例共享限流计数。
- Redis 不可达时应用**自动回退内存限流**并打印告警,不会崩服务。
- 若想单进程内存限流即可,可在 `app.environment` 里删掉 `REDIS_URL` 那行。

## 6. 生产上线检查清单

- [ ] 域名 A/AAAA 记录已指向服务器公网 IP;
- [ ] 服务器防火墙/安全组放行 80、443(TCP);
- [ ] `APP_DOMAIN` 设成真实域名,`Caddyfile` 全局段里填好 ACME 邮箱(可选);
- [ ] 访问 `https://<域名>/` 登录后,浏览器开发者工具看 `Set-Cookie`:
      cookie 应带 `Secure` 标记,响应头应含 `Strict-Transport-Security`;
- [ ] 确认 `docker compose ps` 三服务都 `Up`,`app` 的 HEALTHCHECK 通过。

## 7. 常见问题

| 现象 | 处理 |
|---|---|
| 本地 https://localhost 提示不安全 | 正常,是 Caddy 内部 CA(自签性质)。点"继续访问"即可;要消除需用真实域名 |
| 改代码后不生效 | 需重建镜像: `docker compose up -d --build` |
| 端口被占用起不来 | 见第 4 节改宿主端口 |
| caddy 日志报证书错误 | 域名解析没通或 80/443 未放行;或本地用了公网域名但没 DNS |
| 想彻底重置(含清库) | `docker compose down -v`(⚠️ 会删掉 app.db 和 .secret,全员需重注册/重登录) |

## 8. 12306 实时查票代理（`train_proxy` 服务）

`docker-compose.yml` 已内置 `train_proxy` 服务:它用 Playwright 真实浏览器指纹绕过 12306 反爬,并通过 Caddy 的 `/train` 子路径对外暴露(复用同一证书,无需额外域名)。

> ⚠️ **关键前提**:12306 官方按**出口 IP** 拦截。若本节点 IP 被 12306 封锁,代理会优雅降级(返回 error 而非崩溃),但**取不到实时车次**。代理必须跑在"未被 12306 封锁的节点"(如干净的国内/住宅出口、你自己的服务器)才能真正出数据。

### 8.1 启动与鉴权

```bash
# 设置代理 Token(强烈建议公网开启,避免被他人滥用刷 12306)
# 可与 daily-report「数据源配置」页的「12306 代理 Token」保持一致
export TRAIN_PROXY_TOKEN="你的强Token"

APP_DOMAIN=your.domain.com docker compose up -d --build
```

Token 不设置也能跑(此时无鉴权,仅依赖 Caddy 的 GET-only + 路径收敛做最小化防护)。

### 8.2 把 daily-report 指向代理

有两种接法,二选一:

**(A) 同节点内网直连(推荐,无 TLS 开销)**
在 daily-report「设置 → 数据源配置」页填写:
- 12306 代理地址: `http://train_proxy:8799`(Docker 内网 DNS,仅同 compose 内可达)
- 12306 代理 Token: 与 `TRAIN_PROXY_TOKEN` 一致

**(B) 走公网 Caddy 子路径(跨主机/外网调用)**
- 12306 代理地址: `https://<你的域名>/train`
- 12306 代理 Token: 与 `TRAIN_PROXY_TOKEN` 一致

Caddy 已限制:仅 `GET /train/query` 与 `GET /train/health` 对外,其余 `/train/*` 返回 404,非 GET 返回 405。日常生成行程时,旅游智能体会直出实时车次/余票。

### 8.3 单独验证代理是否通

```bash
# 容器内直接打(需先 docker compose exec 进 caddy/app 网络,或临时 publish 端口)
curl -G "http://train_proxy:8799/query" \
  --data-urlencode "from=北京" --data-urlencode "to=上海" \
  --data-urlencode "date=2026-09-17" --data-urlencode "token=你的Token"
# 若返回 {"ok":true,"trains":[...]} 即实时可用;若 {"ok":false,"error":"blocked_by_12306_waf"} 说明本节点 IP 被封
```
