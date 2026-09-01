// 日报工作台 —— Web 服务（零依赖，仅用 Node 内置模块）
// 含用户注册/登录 + 数据隔离（每人只能访问自己的日报）
// 启动： node server.js   （PORT 默认 8787）
// 云部署： PORT / DATA_DIR 由平台注入；监听 0.0.0.0 供外部访问
// 数据持久化在 DATA_DIR/reports.json 与 DATA_DIR/users.json

const http = require("http");
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

const PORT = process.env.PORT || 8787;
const ROOT = __dirname;
const PUBLIC_DIR = path.join(ROOT, "public");
const DATA_DIR = process.env.DATA_DIR ? path.resolve(process.env.DATA_DIR) : path.join(ROOT, "data");
const DATA_FILE = path.join(DATA_DIR, "reports.json");
const USERS_FILE = path.join(DATA_DIR, "users.json");
const BLOGS_FILE = path.join(DATA_DIR, "blogs.json");
const SECRET_FILE = path.join(DATA_DIR, ".secret");

function ensureStore() {
  if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });
  if (!fs.existsSync(DATA_FILE)) fs.writeFileSync(DATA_FILE, "[]");
  if (!fs.existsSync(USERS_FILE)) fs.writeFileSync(USERS_FILE, "[]");
  if (!fs.existsSync(BLOGS_FILE)) fs.writeFileSync(BLOGS_FILE, "[]");
  if (!fs.existsSync(SECRET_FILE)) fs.writeFileSync(SECRET_FILE, crypto.randomBytes(32).toString("hex"));
}
function readAll() {
  try { return JSON.parse(fs.readFileSync(DATA_FILE, "utf8")) || []; }
  catch (e) { return []; }
}
function writeAll(arr) { fs.writeFileSync(DATA_FILE, JSON.stringify(arr, null, 2)); }
function readUsers() {
  try { return JSON.parse(fs.readFileSync(USERS_FILE, "utf8")) || []; }
  catch (e) { return []; }
}
function writeUsers(arr) { fs.writeFileSync(USERS_FILE, JSON.stringify(arr, null, 2)); }
function readBlogs() {
  try { return JSON.parse(fs.readFileSync(BLOGS_FILE, "utf8")) || []; }
  catch (e) { return []; }
}
function writeBlogs(arr) { fs.writeFileSync(BLOGS_FILE, JSON.stringify(arr, null, 2)); }
function getSecret() { try { return fs.readFileSync(SECRET_FILE, "utf8").trim(); } catch (e) { return "dev-secret"; } }
function uid() { return Date.now().toString(36) + Math.random().toString(36).slice(2, 7); }

/* ---------- 密码哈希（scrypt + 随机盐） ---------- */
function hashPassword(pw) {
  const salt = crypto.randomBytes(16).toString("hex");
  const hash = crypto.scryptSync(pw, salt, 64).toString("hex");
  return { salt, hash };
}
function verifyPassword(pw, salt, hash) {
  const h = crypto.scryptSync(pw, salt, 64).toString("hex");
  const a = Buffer.from(h, "hex"), b = Buffer.from(hash, "hex");
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

/* ---------- token（HMAC 签名，无状态） ---------- */
function signToken(userId) {
  const payload = Buffer.from(JSON.stringify({ uid: userId, exp: Date.now() + 1000 * 60 * 60 * 24 * 30 })).toString("base64url");
  const sig = crypto.createHmac("sha256", getSecret()).update(payload).digest("base64url");
  return payload + "." + sig;
}
function verifyToken(token) {
  if (!token || typeof token !== "string" || !token.includes(".")) return null;
  const parts = token.split(".");
  if (parts.length !== 2) return null;
  const expect = crypto.createHmac("sha256", getSecret()).update(parts[0]).digest("base64url");
  const a = Buffer.from(parts[1]), b = Buffer.from(expect);
  if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) return null;
  try {
    const data = JSON.parse(Buffer.from(parts[0], "base64url").toString());
    if (!data.exp || data.exp < Date.now()) return null;
    return data.uid;
  } catch (e) { return null; }
}
function getUser(req) {
  const ah = req.headers["authorization"] || "";
  const m = ah.match(/^Bearer\s+(.+)$/i);
  if (!m) return null;
  return verifyToken(m[1]);
}

/* ---------- 响应/请求辅助 ---------- */
function sendJSON(res, code, obj) {
  res.writeHead(code, { "Content-Type": "application/json; charset=utf-8" });
  res.end(JSON.stringify(obj));
}
function readBody(req, cb) {
  let buf = "";
  req.on("data", (d) => { buf += d; if (buf.length > 5e5) req.destroy(); });
  req.on("end", () => { try { cb(JSON.parse(buf || "{}")); } catch (e) { cb({}); } });
}
const MIME = { ".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8", ".png": "image/png", ".svg": "image/svg+xml" };

/* ---------- 认证接口 ---------- */
function handleAuth(req, res, url, body) {
  // 注册
  if (req.method === "POST" && url === "/api/auth/register") {
    const username = (body.username || "").trim();
    const password = body.password || "";
    if (username.length < 3 || username.length > 20) return sendJSON(res, 400, { error: "用户名需 3-20 位" });
    if (password.length < 6) return sendJSON(res, 400, { error: "密码至少 6 位" });
    const users = readUsers();
    if (users.some((u) => u.username.toLowerCase() === username.toLowerCase())) {
      return sendJSON(res, 409, { error: "该用户名已被注册" });
    }
    const wasEmpty = users.length === 0;
    const hp = hashPassword(password);
    const u = { id: uid(), username, salt: hp.salt, hash: hp.hash, createdAt: Date.now() };
    users.push(u); writeUsers(users);
    // 首个用户继承升级前无主数据（兼容无账号旧版）
    if (wasEmpty) {
      const all = readAll();
      let changed = false;
      all.forEach((r) => { if (!r.userId) { r.userId = u.id; changed = true; } });
      if (changed) writeAll(all);
    }
    return sendJSON(res, 200, { token: signToken(u.id), user: { id: u.id, username: u.username } });
  }
  // 登录
  if (req.method === "POST" && url === "/api/auth/login") {
    const username = (body.username || "").trim();
    const password = body.password || "";
    const users = readUsers();
    const u = users.find((x) => x.username.toLowerCase() === username.toLowerCase());
    if (!u || !verifyPassword(password, u.salt, u.hash)) return sendJSON(res, 401, { error: "用户名或密码错误" });
    return sendJSON(res, 200, { token: signToken(u.id), user: { id: u.id, username: u.username } });
  }
  // 当前用户
  if (req.method === "GET" && url === "/api/auth/me") {
    const uidv = getUser(req);
    if (!uidv) return sendJSON(res, 401, { error: "未登录" });
    const u = readUsers().find((x) => x.id === uidv);
    if (!u) return sendJSON(res, 401, { error: "用户不存在" });
    return sendJSON(res, 200, { user: { id: u.id, username: u.username } });
  }
  return sendJSON(res, 404, { error: "unknown auth endpoint" });
}

/* ---------- 博客接口（列表/详情公开；写/改/删需登录且仅限本人） ---------- */
function blogPublic(b) {
  return {
    id: b.id, title: b.title, summary: b.summary || "",
    tags: b.tags || [], authorId: b.authorId, authorName: b.authorName,
    visibility: b.visibility || "public", cover: b.cover || "", category: b.category || "",
    createdAt: b.createdAt, updatedAt: b.updatedAt
  };
}
function handleBlogs(req, res, url) {
  // 列表（公开，无需登录；scope=public 仅返回公开笔记，否则返回全部）
  if (req.method === "GET" && url === "/api/blogs") {
    const qp = new URLSearchParams((req.url.split("?")[1] || ""));
    let blogs = readBlogs();
    if (qp.get("scope") === "public") blogs = blogs.filter(function (b) { return b.visibility !== "private"; });
    const list = blogs.sort(function (a, b) { return (b.createdAt || 0) - (a.createdAt || 0); }).map(blogPublic);
    return sendJSON(res, 200, list);
  }
  // 单篇（公开，无需登录）
  const m = url.match(/^\/api\/blogs\/([\w\-]+)$/);
  if (m) {
    const id = m[1];
    if (req.method === "GET") {
      const b = readBlogs().find(function (x) { return x.id === id; });
      if (!b) return sendJSON(res, 404, { error: "not found" });
      return sendJSON(res, 200, b);
    }
    // PUT / DELETE 需要登录且只能操作自己的
    const uidv = getUser(req);
    if (!uidv) return sendJSON(res, 401, { error: "未登录" });
    const u = readUsers().find(function (x) { return x.id === uidv; });
    if (!u) return sendJSON(res, 401, { error: "用户不存在" });
    const blogs = readBlogs();
    const idx = blogs.findIndex(function (x) { return x.id === id; });
    if (idx < 0) return sendJSON(res, 404, { error: "not found" });
    if (blogs[idx].authorId !== uidv) return sendJSON(res, 403, { error: "只能修改/删除自己的博客" });
    if (req.method === "DELETE") {
      writeBlogs(blogs.filter(function (x) { return x.id !== id; }));
      return sendJSON(res, 200, { ok: true });
    }
    if (req.method === "PUT") {
      return readBody(req, function (body) {
        const b = blogs[idx];
        if (body.title != null) b.title = (body.title || "").trim() || b.title;
        if (body.summary != null) b.summary = (body.summary || "").trim();
        if (body.content != null) b.content = body.content;
        if (body.category != null) b.category = (body.category || "").trim();
        if (body.cover != null) b.cover = (body.cover || "").trim();
        if (body.visibility != null) b.visibility = body.visibility === "private" ? "private" : "public";
        if (body.tags != null) b.tags = Array.isArray(body.tags) ? body.tags.map(function (t) { return (t || "").trim(); }).filter(Boolean) : b.tags;
        b.updatedAt = Date.now();
        writeBlogs(blogs);
        sendJSON(res, 200, b);
      });
    }
  }
  // 创建（需要登录）
  if (req.method === "POST" && url === "/api/blogs") {
    const uidv = getUser(req);
    if (!uidv) return sendJSON(res, 401, { error: "未登录" });
    const u = readUsers().find(function (x) { return x.id === uidv; });
    if (!u) return sendJSON(res, 401, { error: "用户不存在" });
    return readBody(req, function (body) {
      const title = (body.title || "").trim();
      const content = (body.content || "").trim();
      if (!title) return sendJSON(res, 400, { error: "标题不能为空" });
      if (!content) return sendJSON(res, 400, { error: "正文不能为空" });
      const now = Date.now();
      const b = {
        id: uid(), title: title, summary: (body.summary || "").trim(),
        content: content,
        category: (body.category || "").trim(),
        tags: Array.isArray(body.tags) ? body.tags.map(function (t) { return (t || "").trim(); }).filter(Boolean) : [],
        visibility: body.visibility === "private" ? "private" : "public",
        cover: (body.cover || "").trim(),
        authorId: u.id, authorName: u.username, createdAt: now, updatedAt: now
      };
      const blogs = readBlogs(); blogs.push(b); writeBlogs(blogs);
      sendJSON(res, 200, b);
    });
  }
  return sendJSON(res, 404, { error: "unknown blog endpoint" });
}

/* ---------- 日报接口（强制登录 + 按用户隔离） ---------- */
function handleApi(req, res, url) {
  const uidv = getUser(req);
  if (!uidv) return sendJSON(res, 401, { error: "未登录" });

  // 列表（仅自己的）
  if (req.method === "GET" && url === "/api/reports") {
    return sendJSON(res, 200, readAll().filter((r) => r.userId === uidv));
  }
  // 新建（归属当前用户）
  if (req.method === "POST" && url === "/api/reports") {
    return readBody(req, (body) => {
      const arr = readAll();
      const now = Date.now();
      const r = Object.assign({}, body);
      r.id = r.id || uid();
      r.userId = uidv;
      r.createdAt = r.createdAt || now;
      r.updatedAt = now;
      arr.push(r);
      writeAll(arr);
      sendJSON(res, 200, r);
    });
  }
  // 单条：更新 / 删除（校验归属）
  const m = url.match(/^\/api\/reports\/([\w\-]+)$/);
  if (m) {
    const id = m[1];
    if (req.method === "PUT") {
      return readBody(req, (body) => {
        const arr = readAll();
        const i = arr.findIndex((x) => x.id === id);
        if (i < 0) return sendJSON(res, 404, { error: "not found" });
        if (arr[i].userId !== uidv) return sendJSON(res, 403, { error: "无权修改" });
        arr[i] = Object.assign({}, arr[i], body, { id: id, userId: uidv, updatedAt: Date.now() });
        writeAll(arr);
        sendJSON(res, 200, arr[i]);
      });
    }
    if (req.method === "DELETE") {
      const arr = readAll();
      const t = arr.find((x) => x.id === id);
      if (t && t.userId !== uidv) return sendJSON(res, 403, { error: "无权删除" });
      writeAll(arr.filter((x) => x.id !== id));
      return sendJSON(res, 200, { ok: true });
    }
  }
  return sendJSON(res, 404, { error: "unknown endpoint" });
}

const server = http.createServer((req, res) => {
  const url = req.url.split("?")[0];
  if (url === "/api/health") {
    res.writeHead(200, { "Content-Type": "application/json" });
    return res.end(JSON.stringify({ ok: true, time: Date.now() }));
  }
  if (url.startsWith("/api/auth/")) {
    return readBody(req, (body) => handleAuth(req, res, url, body));
  }
  if (url.startsWith("/api/blogs")) {
    return handleBlogs(req, res, url);
  }
  if (url.startsWith("/api/")) {
    return handleApi(req, res, url);
  }

  let urlPath = url;
  if (urlPath === "/") urlPath = "/index.html";
  const safe = path.normalize(urlPath).replace(/^(\.\.[\/\\])+/, "");
  const filePath = path.join(PUBLIC_DIR, safe);
  if (!filePath.startsWith(PUBLIC_DIR)) {
    res.writeHead(403); return res.end("forbidden");
  }
  fs.readFile(filePath, (err, data) => {
    if (err) { res.writeHead(404); return res.end("not found"); }
    const ext = path.extname(filePath).toLowerCase();
    res.writeHead(200, { "Content-Type": MIME[ext] || "application/octet-stream" });
    res.end(data);
  });
});

ensureStore();
server.listen(PORT, "0.0.0.0", () => {
  console.log("日报工作台已启动： http://0.0.0.0:" + PORT + "  (PID " + process.pid + ")");
});
