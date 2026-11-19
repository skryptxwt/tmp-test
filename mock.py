import argparse
from concurrent import futures
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time

import grpc
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
            if change.kind != pb.TENANT and not change.asset_id:
                raise ValueError("资产变更需要非零 asset_id")
            if change.kind != pb.TENANT and change.spread_sec:
                raise ValueError("spread_sec 仅适用于 TENANT")
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
                    if change.kind == pb.TENANT:
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


def main():
    parser = argparse.ArgumentParser(description="IdtProxy 资产变更 gRPC mock")
    parser.add_argument("--db", default="changes.sqlite3", help="服务和写入命令使用同一数据库")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--listen", default="127.0.0.1:50051")
    serve.add_argument("--web-port", type=int, default=8765, help="本机配置页端口，0 表示关闭")
    serve.add_argument("--web-host", default="127.0.0.1", help="配置页监听地址")
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
    grpc_port = server.add_insecure_port(args.listen)
    server.start()
    print(f"IdtProxy 监听 {args.listen}，数据库 {Path(args.db).resolve()}", flush=True)
    try:
        if args.web_port:
            host = args.listen.rsplit(":", 1)[0]
            host = {"0.0.0.0": "127.0.0.1", "[::]": "[::1]", "": "127.0.0.1"}.get(host, host)
            target = f"{host}:{grpc_port}"
            with grpc.insecure_channel(target, options=[("grpc.enable_http_proxy", 0)]) as channel:
                with make_console(args.web_port, rpc.IdtProxyStub(channel), target,
                                  lambda rows: write_changes(args.db, rows), args.web_host) as http:
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
