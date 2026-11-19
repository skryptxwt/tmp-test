import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import grpc
from google.protobuf.json_format import MessageToDict, ParseDict, ParseError

import idt_proxy_pb2 as pb


WEB = Path(__file__).with_name("web")
FILES = {"/": ("index.html", "text/html"), "/console.css": ("console.css", "text/css"),
         "/console.js": ("console.js", "text/javascript")}


def make_console(port, stub, target, write_changes, host="127.0.0.1", *, save_source=None):
    queries = {
        "ListChangedTenants": (pb.ListChangedTenantsReq, stub.ListChangedTenants),
        "ListAssetChanges": (pb.ListAssetChangesReq, stub.ListAssetChanges),
        "AssetQueryByIp": (pb.AssetQueryByIpReq, stub.AssetQueryByIp),
        "AuthQueryByIp": (pb.AuthQueryByIpReq, stub.AuthQueryByIp),
    }

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

        def query(self, method, data):
            if not isinstance(data, dict):
                self.respond(400, {"error": "请求体须为 JSON 对象", "status": "INVALID_ARGUMENT"})
                return
            request_type, call = queries[method]
            try:
                result = call(ParseDict(data, request_type()), timeout=5)
                self.respond(200, {"target": target, "status": "OK", "response": MessageToDict(
                    result, preserving_proto_field_name=True, always_print_fields_with_no_presence=True)})
            except (ValueError, ParseError) as exc:
                self.respond(400, {"error": str(exc), "status": "INVALID_ARGUMENT"})
            except grpc.RpcError as exc:
                status = {grpc.StatusCode.INVALID_ARGUMENT: 400, grpc.StatusCode.NOT_FOUND: 404}
                self.respond(status.get(exc.code(), 503), {"error": exc.details(), "status": exc.code().name})

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
            method = params.get("method", ["assets"])[0]
            if method in ("tenants", "assets"):
                data = {k: params[k][0] for k in ("cursor", "until_cursor", "limit") if k in params}
                if method == "assets":
                    data["tenant_ids"] = params.get("tenant_id", [])
                self.query("ListAssetChanges" if method == "assets" else "ListChangedTenants", data)
            elif method in ("AssetQueryByIp", "AuthQueryByIp"):
                self.query(method, {k: params[k][0] for k in ("ip", "tenant_id") if k in params})
            else:
                self.respond(400, {"error": "未知的查询接口", "status": "INVALID_ARGUMENT"})

        def do_POST(self):
            if not self.local_request():
                return
            handlers = {"/api/changes": write_changes, "/api/source": save_source}
            path = urlsplit(self.path).path
            handler = handlers.get(path)
            method = path.removeprefix("/api/rpc/") if path.startswith("/api/rpc/") else None
            if handler is None and method not in queries:
                self.respond(404, {"error": "未知接口", "status": "NOT_FOUND"})
                return
            if self.headers.get_content_type() != "application/json":
                self.respond(415, {"error": "需要 application/json"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 1_048_576:
                    self.respond(413, {"error": "请输入 1 MB 以内的 JSON"})
                    return
                data = json.loads(self.rfile.read(size))
                if method in queries:
                    self.query(method, data)
                else:
                    self.respond(201, handler(data))
            except (ValueError, RecursionError) as exc:
                self.respond(400, {"error": str(exc)})
            except sqlite3.Error:
                self.respond(503, {"error": "变更日志暂时无法写入，请重试"})

    return ThreadingHTTPServer((host, port), Handler)
