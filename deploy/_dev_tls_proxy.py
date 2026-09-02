#!/usr/bin/env python3
# 本地 HTTPS 验证用反向代理（非生产）：在 127.0.0.1:8443 终止 TLS，
# 转发到 127.0.0.1:8787，并加上 X-Forwarded-Proto: https / X-Forwarded-For，
# 使应用据此下发 Secure cookie + Strict-Transport-Security。
# 生产环境请改用 deploy/Caddyfile 或 deploy/nginx.conf。
import ssl
import http.server
import http.client
import socketserver
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CERT = str(ROOT / "certs" / "localhost.crt")
KEY = str(ROOT / "certs" / "localhost.key")
UP_HOST, UP_PORT = "127.0.0.1", 8787
LISTEN_HOST, LISTEN_PORT = "127.0.0.1", 8443


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "dev-tls-proxy/1.0"

    def _proxy(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else None
        fwd = {k: v for k, v in self.headers.items()}
        fwd["X-Forwarded-Proto"] = "https"
        fwd["X-Forwarded-For"] = self.client_address[0]
        fwd.pop("Host", None)  # 由 http.client 按上游重设
        try:
            conn = http.client.HTTPConnection(UP_HOST, UP_PORT, timeout=30)
            conn.request(self.command, self.path, body=body, headers=fwd)
            resp = conn.getresponse()
            data = resp.read()
        finally:
            conn.close()
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() in ("transfer-encoding", "connection"):
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def do_GET(self): self._proxy()
    def do_POST(self): self._proxy()
    def do_PUT(self): self._proxy()
    def do_DELETE(self): self._proxy()
    def do_OPTIONS(self): self._proxy()
    def log_message(self, *a): pass


class TLSProxy(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=CERT, keyfile=KEY)
        self.socket = ctx.wrap_socket(self.socket, server_side=True)


if __name__ == "__main__":
    srv = TLSProxy((LISTEN_HOST, LISTEN_PORT), Handler)
    print(f"HTTPS 反向代理已启动: https://{LISTEN_HOST}:{LISTEN_PORT} -> {UP_HOST}:{UP_PORT}")
    print("（本地验证用，生产请改用 deploy/Caddyfile 或 deploy/nginx.conf）")
    srv.serve_forever()
