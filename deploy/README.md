# 部署加固指南（反向代理 + HTTPS + 共享限速）

本目录提供把「日报工作台」安全上线所需的部署配置。应用本体仍是 `uvicorn app:app --port 8787`，
**不要直接把 8787 暴露在公网**，应放在反向代理之后，由代理终止 TLS。

---

## 1. 为什么需要这一层

前几轮安全加固已把应用做到：

- 默认关闭公开注册 + 注册/登录速率限制；
- 全套安全响应头（CSP / X-Frame-Options / nosniff / Referrer-Policy / HSTS）；
- 认证 token 改为 **HttpOnly + SameSite=Lax cookie**，根治 XSS 窃 token、缓解 CSRF。

但还有两个“靠部署层才能闭环”的点：

1. **Secure cookie 与 HSTS 必须跑在 HTTPS 上才生效。**
   应用通过请求头 `X-Forwarded-Proto: https` 判断是否处于 TLS 之下：
   - 是 → 下发 `Set-Cookie ... Secure`，并下发 `Strict-Transport-Security`；
   - 否（纯 HTTP）→ 不下发，避免本地开发被误锁。
   所以只要反代正确设置了 `X-Forwarded-Proto`，上面的代码会自动切换，**无需改代码**。

2. **速率限制默认是单进程内存实现。**
   多 worker / 多实例部署时各进程各自计数，会放大放行额度。设置环境变量
   `REDIS_URL=redis://host:6379/0` 后，限流自动改为 Redis 共享存储；Redis 不可达时自动回退内存，不影响服务。

---

## 2. 反向代理方案（二选一）

### 方案 A：Caddy（推荐，零配置拿证书）

见 `Caddyfile`：

```bash
# 本地测试（Caddy 内部 CA 自动为 localhost 签发证书，浏览器提示“不安全”属正常）
caddy run --config deploy/Caddyfile

# 生产：把 Caddyfile 里 production 段注释取消，换成你的域名，Caddy 自动 ACME 申请并续期
```

### 方案 B：nginx + 自签证书（本目录已备好证书）

见 `nginx.conf` 与 `certs/localhost.crt`、`certs/localhost.key`（已用 openssl 生成）：

```bash
# 在项目根目录下启动，使相对证书路径生效
nginx -c "$(pwd)/deploy/nginx.conf"
```

> 自签证书仅用于本地测试。生产请把 `ssl_certificate` / `ssl_certificate_key` 换成
> 受信任证书（Let's Encrypt / 你的企业 CA），并把监听端口改为 443。

两种方案都会自动设置 `X-Forwarded-Proto` / `X-Forwarded-For`，应用据此激活 Secure cookie 与 HSTS。

### 重新生成自签测试证书（deploy/certs 已被 .gitignore 忽略）

```bash
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout deploy/certs/localhost.key \
  -out    deploy/certs/localhost.crt \
  -days 825 -subj "/CN=localhost" \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
```

> 生产请勿使用自签证书：改用 Let's Encrypt（Caddy 自动 ACME）或你的企业 CA 签发的证书。

---

## 3. 共享速率限制（Redis）

安装依赖（已在 `requirements.txt` 锁定 `redis==5.2.1`）后，设置环境变量即可启用：

```bash
export REDIS_URL="redis://127.0.0.1:6379/0"
uvicorn app:app --host 127.0.0.1 --port 8787
```

- 未设置 `REDIS_URL` → 使用进程内内存限流（单进程足够）。
- 设置后且 Redis 可达 → 使用 Redis 滑动窗口（多实例共享计数）。
- Redis 不可达 → 自动回退内存限流，并在 stderr 打印一行告警，不影响服务。

当前限流阈值：注册 5 次/600s、登录 12 次/300s（按客户端 IP，尊重 `X-Forwarded-For`）。

---

## 4. 上线检查清单

- [ ] 应用仅监听 `127.0.0.1:8787`，不被公网直接访问；
- [ ] 反代监听 443（生产）/ 8443（测试），终止 TLS；
- [ ] 反代设置 `X-Forwarded-Proto: https`（Caddy/nginx 默认会）；
- [ ] 访问 `https://<域名>/` 登录后，`Set-Cookie` 含 `Secure`，响应头含 `Strict-Transport-Security`；
- [ ] （多实例时）设置 `REDIS_URL` 指向可达的 Redis；
- [ ] `requirements.txt` 已锁定全部依赖，并跑过 `pip-audit`（历史批次已验证 No known vulnerabilities）。
