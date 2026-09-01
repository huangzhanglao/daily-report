// 日报工作台 —— Web 服务（零依赖，仅用 Node 内置模块）
// 本地启动： node server.js   （PORT 默认 8787）
// 云部署：   PORT / DATA_DIR 由平台注入；监听 0.0.0.0 供外部访问
// 数据持久化在 DATA_DIR/reports.json（云上请挂载持久卷到该目录）

const http = require("http");
const fs = require("fs");
const path = require("path");

const PORT = process.env.PORT || 8787;
const ROOT = __dirname;
const PUBLIC_DIR = path.join(ROOT, "public");
const DATA_DIR = process.env.DATA_DIR ? path.resolve(process.env.DATA_DIR) : path.join(ROOT, "data");
const DATA_FILE = path.join(DATA_DIR, "reports.json");

function ensureStore() {
  if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });
  if (!fs.existsSync(DATA_FILE)) fs.writeFileSync(DATA_FILE, "[]");
}
function readAll() {
  try { return JSON.parse(fs.readFileSync(DATA_FILE, "utf8")) || []; }
  catch (e) { return []; }
}
function writeAll(arr) {
  fs.writeFileSync(DATA_FILE, JSON.stringify(arr, null, 2));
}
function uid() { return Date.now().toString(36) + Math.random().toString(36).slice(2, 7); }

const MIME = { ".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8", ".png": "image/png", ".svg": "image/svg+xml" };

function sendJSON(res, code, obj) {
  res.writeHead(code, { "Content-Type": "application/json; charset=utf-8" });
  res.end(JSON.stringify(obj));
}
function readBody(req, cb) {
  let buf = "";
  req.on("data", (d) => { buf += d; if (buf.length > 5e6) req.destroy(); });
  req.on("end", () => { try { cb(JSON.parse(buf || "{}")); } catch (e) { cb({}); } });
}

function handleApi(req, res) {
  const url = req.url;
  // 列表
  if (req.method === "GET" && url === "/api/reports") {
    return sendJSON(res, 200, readAll());
  }
  // 新建
  if (req.method === "POST" && url === "/api/reports") {
    return readBody(req, (body) => {
      const arr = readAll();
      const now = Date.now();
      const r = Object.assign({}, body);
      r.id = r.id || uid();
      r.createdAt = r.createdAt || now;
      r.updatedAt = now;
      arr.push(r);
      writeAll(arr);
      sendJSON(res, 200, r);
    });
  }
  // 单条：更新 / 删除
  const m = url.match(/^\/api\/reports\/([\w\-]+)$/);
  if (m) {
    const id = m[1];
    if (req.method === "PUT") {
      return readBody(req, (body) => {
        const arr = readAll();
        const i = arr.findIndex((x) => x.id === id);
        if (i < 0) return sendJSON(res, 404, { error: "not found" });
        arr[i] = Object.assign({}, arr[i], body, { id: id, updatedAt: Date.now() });
        writeAll(arr);
        sendJSON(res, 200, arr[i]);
      });
    }
    if (req.method === "DELETE") {
      const arr = readAll().filter((x) => x.id !== id);
      writeAll(arr);
      return sendJSON(res, 200, { ok: true });
    }
  }
  sendJSON(res, 404, { error: "unknown endpoint" });
}

const server = http.createServer((req, res) => {
  if (req.url === "/api/health") {
    res.writeHead(200, { "Content-Type": "application/json" });
    return res.end(JSON.stringify({ ok: true, time: Date.now() }));
  }
  if (req.url.startsWith("/api/")) return handleApi(req, res);

  let urlPath = req.url.split("?")[0];
  if (urlPath === "/") urlPath = "/index.html";
  // 防目录穿越
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
