import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import grpc
from google.protobuf.json_format import MessageToDict

import idt_proxy_pb2 as pb


WEB = Path(__file__).with_name("web")
FILES = {"/": ("index.html", "text/html"), "/console.css": ("console.css", "text/css"),
         "/console.js": ("console.js", "text/javascript")}


def make_console(port, stub, target, write_changes, host="127.0.0.1", *, save_source=None):
    class Handler(BaseHTTPRequestHandler):
        def send_body(self, code, content_type, data):
            self.send_response(code)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def respond(self, code, data):
            self.send_body(code, "application/json", json.dumps(data, ensure_ascii=False).encode())

        def local_request(self):
            authority = self.headers.get("Host", "")
            # 本机模式只接收回环 Host；显式对外监听时允许容器端口映射。
            local_only = host in ("localhost", "127.0.0.1")
            allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            origin = self.headers.get("Origin")
            if (not authority or (local_only and authority not in allowed)
                    or (origin and origin not in (f"http://{authority}", f"https://{authority}"))):
                self.respond(403, {"error": "请从当前服务的配置页操作"})
                return False
            return True

        def do_GET(self):
            if not self.local_request():
                return
            url = urlsplit(self.path)
            if url.path in FILES:
                name, mime = FILES[url.path]
                self.send_body(200, mime, (WEB / name).read_bytes())
                return
            if url.path != "/api/preview":
                self.send_error(404)
                return
            params = parse_qs(url.query)
            window = {key: params.get(key, [""])[0] for key in ("cursor", "until_cursor")}
            method = params.get("method", ["assets"])[0]
            try:
                window["limit"] = int(params.get("limit", ["0"])[0])
                if method == "tenants":
                    result = stub.ListChangedTenants(pb.ListChangedTenantsReq(**window), timeout=5)
                elif method == "assets":
                    req = pb.ListAssetChangesReq(**window, tenant_ids=params.get("tenant_id", []))
                    result = stub.ListAssetChanges(req, timeout=5)
                elif method in ("AssetQueryByIp", "AuthQueryByIp"):
                    key = {name: params.get(name, [""])[0] for name in ("ip", "tenant_id")}
                    if method == "AssetQueryByIp":
                        result = stub.AssetQueryByIp(pb.AssetQueryByIpReq(**key), timeout=5)
                    else:
                        result = stub.AuthQueryByIp(pb.AuthQueryByIpReq(**key), timeout=5)
                else:
                    raise ValueError("未知的查询接口")
                self.respond(200, {"target": target, "status": "OK", "response": MessageToDict(
                    result, preserving_proto_field_name=True, always_print_fields_with_no_presence=True)})
            except ValueError as exc:
                self.respond(400, {"error": str(exc), "status": "INVALID_ARGUMENT"})
            except grpc.RpcError as exc:
                status = {grpc.StatusCode.INVALID_ARGUMENT: 400, grpc.StatusCode.NOT_FOUND: 404}
                self.respond(status.get(exc.code(), 503),
                             {"error": exc.details(), "status": exc.code().name})

        def do_POST(self):
            if not self.local_request():
                return
            handlers = {"/api/changes": write_changes, "/api/source": save_source}
            handler = handlers.get(self.path)
            if handler is None:
                self.send_error(404)
                return
            if self.headers.get_content_type() != "application/json":
                self.respond(415, {"error": "需要 application/json"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 1_048_576:
                    self.respond(413, {"error": "请输入 1 MB 以内的 JSON"})
                    return
                result = handler(json.loads(self.rfile.read(size)))
                self.respond(201, result)
            except (ValueError, RecursionError) as exc:
                self.respond(400, {"error": str(exc)})
            except sqlite3.Error:
                self.respond(503, {"error": "变更日志暂时无法写入，请重试"})

    return ThreadingHTTPServer((host, port), Handler)
