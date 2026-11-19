import argparse
from concurrent import futures
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import time

import grpc
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.protobuf import json_format

import idt_proxy_pb2 as pb
import idt_proxy_pb2_grpc as rpc
from web_console import make_console


DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
SCAN_LIMIT = 2000


def init_db(db):
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS events "
            "(seq INTEGER PRIMARY KEY AUTOINCREMENT, change BLOB NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS source_responses "
            "(method TEXT, tenant TEXT, ip TEXT, response BLOB NOT NULL, PRIMARY KEY(method, tenant, ip))"
        )


def parse_changes(rows):
    if not isinstance(rows, list) or not rows:
        raise ValueError("文件应包含非空的 AssetChange 数组")
    changes = []
    for index, row in enumerate(rows, start=1):
        try:
            if not isinstance(row, dict):
                raise ValueError("记录应为 JSON 对象")
            change = json_format.ParseDict(row, pb.AssetChange())
            if not change.tenant_id.strip():
                raise ValueError("缺少 tenant_id")
            if change.kind not in pb.AssetChangeKind.values():
                raise ValueError("未知的 kind")
            if change.kind == pb.TENANT and change.asset_id:
                raise ValueError("TENANT 记录的 asset_id 应为 0")
            if change.kind != pb.TENANT and row.get("asset_id", row.get("assetId")) is None:
                raise ValueError("需要 asset_id，显式指定 0 表示整租户")
            if change.asset_id and change.spread_sec:
                raise ValueError("spread_sec 仅适用于整租户变更")
            if "changed_at_ms" not in row and "changedAtMs" not in row:
                change.changed_at_ms = time.time_ns() // 1_000_000
            for field in (change.ips, change.attrs, change.endpoint_keys):
                field[:] = list(dict.fromkeys(field))
            change.macs[:] = sorted({mac.lower() for mac in change.macs})
            devices = sorted({(dev.dev_id, dev.dev_type) for dev in change.origin_devs})
            del change.origin_devs[:]
            for dev_id, dev_type in devices:
                change.origin_devs.add(dev_id=dev_id, dev_type=dev_type)
            changes.append((change.SerializeToString(),))
        except (ValueError, json_format.ParseError) as exc:
            raise ValueError(f"第 {index} 条记录有误：{exc}") from exc
    return changes


def write_changes(db, rows, keep_last=None):
    changes = parse_changes(rows)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.executemany("INSERT INTO events(change) VALUES (?)", changes)
        head = conn.execute("SELECT MAX(seq) FROM events").fetchone()[0]
        if keep_last is not None:
            conn.execute("DELETE FROM events WHERE seq <= ?", (head - keep_last,))
    return {"count": len(changes), "cursor": str(head - len(changes)), "now_cursor": str(head)}


SOURCE_TYPES = {"AssetQueryByIp": pb.AssetQueryByIpResp, "AuthQueryByIp": pb.AuthQueryByIpResp}


def save_source(db, data):
    if not isinstance(data, dict):
        raise ValueError("需要回源配置对象")
    method, tenant, ip = (data.get(key) for key in ("method", "tenant_id", "ip"))
    if not isinstance(method, str) or method not in SOURCE_TYPES:
        raise ValueError("请选择资产或认证回源接口")
    if not all(isinstance(key, str) and key.strip() for key in (tenant, ip)):
        raise ValueError("需要 tenant_id 和 ip")
    if not isinstance(data.get("response"), dict):
        raise ValueError("response 应为该接口的响应对象")
    try:
        response = json_format.ParseDict(data["response"], SOURCE_TYPES[method]())
    except json_format.ParseError as exc:
        raise ValueError(str(exc)) from exc
    if method == "AssetQueryByIp":
        if response.tenant_id and response.tenant_id != tenant:
            raise ValueError("资产响应的 tenant_id 与查询租户不一致")
        response.tenant_id = tenant
    changes = []
    if "change" in data:
        if not isinstance(data["change"], dict):
            raise ValueError("change 应为 AssetChange 对象")
        event = dict(data["change"])
        if "tenantId" not in event:
            event.setdefault("tenant_id", tenant)
        event.setdefault("ips", [ip])
        if method == "AssetQueryByIp" and "assetId" not in event and event.get("kind") not in ("TENANT", pb.TENANT) and response.aid:
            event.setdefault("asset_id", response.aid)
        changes = parse_changes([event])
        change = pb.AssetChange.FromString(changes[0][0])
        if change.tenant_id != tenant or (change.asset_id and ip not in change.ips):
            raise ValueError("通知必须覆盖本次回源配置的租户和 IP")
        if method == "AssetQueryByIp" and change.asset_id and response.aid and response.aid != change.asset_id:
            raise ValueError("通知的 asset_id 与回源响应 aid 不一致")
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("INSERT INTO source_responses VALUES (?, ?, ?, ?) "
                     "ON CONFLICT(method, tenant, ip) DO UPDATE SET response=excluded.response",
                     (method, tenant, ip, response.SerializeToString()))
        conn.executemany("INSERT INTO events(change) VALUES (?)", changes)
        head = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]
    return {"count": len(changes), "cursor": str(head - len(changes)), "now_cursor": str(head)}


def append(db, path, keep_last):
    rows = json.loads(path.read_text(encoding="utf-8-sig"))
    result = write_changes(db, rows, keep_last)
    print(f"已追加 {result['count']} 条变更，now_cursor={result['now_cursor']}")


def parse_cursor(value, default):
    if not value:
        return default
    if not value.isascii() or not value.isdecimal() or len(value) > 19:
        raise ValueError("游标须为服务返回的非负十进制序号")
    return int(value)


class ChangeService(rpc.IdtProxyServicer):
    def __init__(self, db):
        self.db = db

    def source_response(self, method, req, context):
        with closing(sqlite3.connect(self.db)) as conn:
            row = conn.execute("SELECT response FROM source_responses WHERE method=? AND tenant=? AND ip=?",
                               (method, req.tenant_id, req.ip)).fetchone()
        if row is None:
            context.abort(grpc.StatusCode.NOT_FOUND, "该租户和 IP 尚未配置此接口的回源数据")
        return SOURCE_TYPES[method].FromString(row[0])

    def AssetQueryByIp(self, request, context):
        return self.source_response("AssetQueryByIp", request, context)

    def AuthQueryByIp(self, request, context):
        return self.source_response("AuthQueryByIp", request, context)

    def page(self, req, context, *, summary):
        resp = pb.ListChangedTenantsResp() if summary else pb.ListAssetChangesResp()
        tenant_filter = set() if summary else set(req.tenant_ids)
        limit = min(req.limit or DEFAULT_LIMIT, MAX_LIMIT)
        with closing(sqlite3.connect(self.db)) as conn:
            # 固定读取快照，避免 append 或裁剪发生在读水位和读明细之间。
            conn.execute("BEGIN")
            oldest, head = conn.execute("SELECT MIN(seq), MAX(seq) FROM events").fetchone()
            head = head or 0
            try:
                start = parse_cursor(req.cursor, 0)
                end = parse_cursor(req.until_cursor, head)
                if not start <= end <= head:
                    raise ValueError("需要 cursor <= until_cursor <= now_cursor")
            except ValueError as exc:
                context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
            resp.now_cursor = str(head)
            resp.next_cursor = str(end)
            if oldest is not None and start < end and start < oldest - 1:
                resp.gap = True
                return resp

            tenants = {}
            attrs_by_tenant = {}
            rows = conn.execute(
                "SELECT seq, change FROM events WHERE seq > ? AND seq <= ? "
                "ORDER BY seq LIMIT ?", (start, end, SCAN_LIMIT)
            )
            for scanned, (seq, payload) in enumerate(rows, start=1):
                change = pb.AssetChange.FromString(payload)
                if summary:
                    if change.tenant_id not in tenants:
                        tenants[change.tenant_id] = resp.tenants.add(tenant_id=change.tenant_id)
                        attrs_by_tenant[change.tenant_id] = set()
                    tenant = tenants[change.tenant_id]
                    attrs_by_tenant[change.tenant_id].update(change.attrs)
                    tenant.changed_at_ms = change.changed_at_ms
                    if change.kind == pb.TENANT or change.asset_id == 0:
                        tenant.tenant_wide = True
                        tenant.spread_sec = change.spread_sec
                elif not tenant_filter or change.tenant_id in tenant_filter:
                    resp.changes.append(change)
                count = len(tenants) if summary else len(resp.changes)
                if count == limit or scanned == SCAN_LIMIT:
                    resp.has_more = True
                    resp.next_cursor = str(seq)
                    break
            for tenant_id, tenant in tenants.items():
                tenant.attrs.extend(sorted(attrs_by_tenant[tenant_id]))
        return resp

    def ListChangedTenants(self, request, context):
        return self.page(request, context, summary=True)

    def ListAssetChanges(self, request, context):
        return self.page(request, context, summary=False)


def generate_tls_identity():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "idt-proxy")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("idt-proxy")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return (key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()), certificate.public_bytes(serialization.Encoding.PEM))


def main():
    parser = argparse.ArgumentParser(description="IdtProxy 资产变更 gRPC mock")
    parser.add_argument("--db", default="changes.sqlite3", help="服务和写入命令使用同一数据库")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--listen", default="127.0.0.1:50051")
    serve.add_argument("--web-port", type=int, default=8765, help="本机配置页端口，0 表示关闭")
    serve.add_argument("--web-host", default="127.0.0.1", help="配置页监听地址")
    serve.add_argument("--tls-auto", action="store_true", help="在内存中生成本次运行的 TLS 证书与私钥")
    serve.add_argument("--tls-cert", type=Path, default=os.getenv("IDT_TLS_CERT"),
                       help="gRPC 服务端 PEM 证书链，配合 --tls-key 启用单向 TLS")
    serve.add_argument("--tls-key", type=Path, default=os.getenv("IDT_TLS_KEY"),
                       help="gRPC 服务端 PEM 私钥")
    serve.add_argument("--web-grpc-ca", type=Path, default=os.getenv("IDT_WEB_GRPC_CA"),
                       help="配置页调用 gRPC 时信任的 CA，默认使用提供的证书链")
    serve.add_argument("--web-grpc-name", default=os.getenv("IDT_WEB_GRPC_NAME"),
                       help="配置页访问本机 gRPC 时校验的服务端证书名称")
    add = commands.add_parser("append")
    add.add_argument("file", type=Path)
    add.add_argument("--keep-last", type=int, help="本次写入后只保留最新 N 条，用于测试 gap")
    args = parser.parse_args()
    init_db(args.db)
    if args.command == "append":
        if args.keep_last is not None and args.keep_last < 1:
            parser.error("--keep-last 必须大于 0")
        try:
            append(args.db, args.file, args.keep_last)
        except (ValueError, OSError) as exc:
            parser.exit(1, f"{exc}\n")
        return
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
    rpc.add_IdtProxyServicer_to_server(ChangeService(args.db), server)
    if not 0 <= args.web_port <= 65535:
        parser.error("--web-port 需要 0 至 65535 的整数")
    if bool(args.tls_cert) != bool(args.tls_key):
        parser.error("--tls-cert 和 --tls-key 必须同时提供")
    auto_setting = os.getenv("IDT_TLS_AUTO", "0")
    if auto_setting not in ("0", "1"):
        parser.error("IDT_TLS_AUTO 取 0 或 1")
    auto_tls = args.tls_auto or auto_setting == "1"
    if auto_tls and (args.tls_cert or args.web_grpc_name or args.web_grpc_ca):
        parser.error("自动 TLS 与手动证书、CA 和证书名称配置互斥")
    if (args.web_grpc_ca or args.web_grpc_name) and not args.tls_cert:
        parser.error("配置页 gRPC TLS 选项需要同时配置服务端证书和私钥")
    if auto_tls:
        key, cert = generate_tls_identity()
        roots = cert
        args.web_grpc_name = "idt-proxy"
    elif args.tls_cert:
        try:
            cert = args.tls_cert.read_bytes()
            key = args.tls_key.read_bytes()
            if args.web_port:
                roots = args.web_grpc_ca.read_bytes() if args.web_grpc_ca else cert
        except OSError as exc:
            parser.exit(1, f"无法读取 TLS 文件：{exc}\n")
    tls_enabled = auto_tls or bool(args.tls_cert)
    if tls_enabled:
        credentials = grpc.ssl_server_credentials(((key, cert),), require_client_auth=False)
        grpc_port = server.add_secure_port(args.listen, credentials)
    else:
        grpc_port = server.add_insecure_port(args.listen)
    server.start()
    mode = "单向 TLS" if tls_enabled else "明文"
    print(f"IdtProxy 监听 {args.listen}（{mode}），数据库 {Path(args.db).resolve()}", flush=True)
    try:
        if args.web_port:
            host = args.listen.rsplit(":", 1)[0]
            host = {"0.0.0.0": "127.0.0.1", "[::]": "[::1]", "": "127.0.0.1"}.get(host, host)
            target = f"{host}:{grpc_port}"
            options = [("grpc.enable_http_proxy", 0)]
            if tls_enabled:
                if args.web_grpc_name:
                    options.append(("grpc.ssl_target_name_override", args.web_grpc_name))
                channel = grpc.secure_channel(target, grpc.ssl_channel_credentials(roots), options=options)
            else:
                channel = grpc.insecure_channel(target, options=options)
            with channel:
                with make_console(args.web_port, rpc.IdtProxyStub(channel), target,
                                  lambda rows: write_changes(args.db, rows), args.web_host,
                                  save_source=lambda data: save_source(args.db, data)) as http:
                    print(f"浏览器配置页 http://{args.web_host}:{http.server_port}/", flush=True)
                    http.serve_forever()
        else:
            server.wait_for_termination()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop(0).wait()


if __name__ == "__main__":
    main()
