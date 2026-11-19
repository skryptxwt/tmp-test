from concurrent import futures
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import build_opener, ProxyHandler, Request

import grpc

import mock
import idt_proxy_pb2 as pb
import idt_proxy_pb2_grpc as rpc


class ChangeRpcTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = str(Path(temp.name) / "changes.db")
        mock.init_db(self.db)
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
        rpc.add_IdtProxyServicer_to_server(mock.ChangeService(self.db), server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        self.addCleanup(lambda: server.stop(0).wait())
        channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        self.addCleanup(channel.close)
        self.stub = rpc.IdtProxyStub(channel)
        self.example = Path(__file__).with_name("changes.example.json")

    def append(self, *options):
        subprocess.run(
            [sys.executable, mock.__file__, "--db", self.db, "append", self.example, *options],
            check=True, capture_output=True,
        )

    def test_live_append_and_frozen_window(self):
        self.assertEqual(self.stub.ListChangedTenants(pb.ListChangedTenantsReq()).now_cursor, "0")
        self.append()
        summary = self.stub.ListChangedTenants(pb.ListChangedTenantsReq())
        self.assertEqual(summary.now_cursor, "3")
        self.assertEqual([t.tenant_id for t in summary.tenants], ["tenant-a", "tenant-b"])
        self.assertEqual(list(summary.tenants[0].attrs), ["assetIp", "managed_level"])
        self.assertTrue(summary.tenants[1].tenant_wide)
        self.assertEqual(summary.tenants[1].spread_sec, 120)
        self.append()
        req = pb.ListAssetChangesReq(until_cursor="3", limit=1, tenant_ids=["tenant-a"])
        seen = []
        while True:
            page = self.stub.ListAssetChanges(req, timeout=3)
            self.assertEqual(page.now_cursor, "6")
            seen.extend(page.changes)
            if not page.has_more:
                break
            self.assertGreater(int(page.next_cursor), int(req.cursor or 0))
            req.cursor = page.next_cursor
        self.assertEqual(page.next_cursor, "3")
        self.assertEqual([c.asset_id for c in seen], [1001, 1002])
        self.assertEqual(list(seen[0].macs), ["aa:bb:cc:dd:ee:01"])
        self.assertEqual(seen[0].origin_devs[0].dev_id, "source-01")
        self.assertEqual(self.stub.ListAssetChanges(pb.ListAssetChangesReq(cursor="3")).changes[2].kind, pb.TENANT)

    def test_gap_and_bad_cursor(self):
        self.append("--keep-last", "1")
        for call, request in [
            (self.stub.ListChangedTenants, pb.ListChangedTenantsReq),
            (self.stub.ListAssetChanges, pb.ListAssetChangesReq),
        ]:
            self.assertTrue(call(request(cursor="1")).gap)
            self.assertFalse(call(request(cursor="2")).gap)
            with self.assertRaises(grpc.RpcError) as invalid:
                call(request(cursor="oops"))
            self.assertEqual(invalid.exception.code(), grpc.StatusCode.INVALID_ARGUMENT)

    def test_filtered_empty_page_can_advance(self):
        path = Path(self.db).with_suffix(".json")
        rows = [{"tenant_id": "a", "asset_id": 1}] * mock.SCAN_LIMIT
        rows.append({"tenant_id": "b", "asset_id": 2, "kind": "DELETE"})
        path.write_text(json.dumps(rows), encoding="utf-8")
        mock.append(self.db, path, None)
        req = pb.ListAssetChangesReq(tenant_ids=["b"])
        page = self.stub.ListAssetChanges(req)
        self.assertFalse(page.changes)
        self.assertTrue(page.has_more)
        req.cursor, req.until_cursor = page.next_cursor, page.now_cursor
        last = self.stub.ListAssetChanges(req)
        self.assertFalse(last.has_more)
        self.assertEqual(last.changes[0].kind, pb.DELETE)

    def test_console_and_grpc_share_changes(self):
        http = mock.make_console(0, self.stub, "test", lambda rows: mock.write_changes(self.db, rows),
                                 save_source=lambda data: mock.save_source(self.db, data))
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(http.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(http.shutdown)
        base = f"http://127.0.0.1:{http.server_port}"
        client = build_opener(ProxyHandler({}))

        def post(rows, route="/api/changes"):
            return client.open(Request(base + route, json.dumps(rows).encode(),
                                       {"Content-Type": "application/json"}), timeout=3)

        for route in ("/", "/console.js", "/console.css"):
            with client.open(base + route, timeout=3) as response:
                self.assertEqual(response.status, 200)
        rows = json.loads(self.example.read_text(encoding="utf-8"))
        with self.assertRaises(HTTPError) as invalid:
            post([rows[0], {"tenant_id": "bad"}])
        self.assertEqual(invalid.exception.code, 400)
        with post(rows) as result:
            self.assertEqual(json.load(result), {"count": 3, "cursor": "0", "now_cursor": "3"})
        self.assertEqual(self.stub.ListAssetChanges(pb.ListAssetChangesReq()).changes[0].asset_id, 1001)
        for query, key, expected in [("assets&tenant_id=tenant-a", "changes", 2), ("tenants", "tenants", 2)]:
            with client.open(base + "/api/preview?method=" + query, timeout=3) as result:
                self.assertEqual(len(json.load(result)["response"][key]), expected)
        for method, key in [("assets", "changes"), ("tenants", "tenants")]:
            with client.open(base + f"/api/preview?method={method}&limit=1") as result:
                page = json.load(result)["response"]
                self.assertEqual(len(page[key]), 1)
                self.assertTrue(page["has_more"])
        with self.assertRaises(HTTPError) as bad_window:
            client.open(base + "/api/preview?method=assets&cursor=99")
        self.assertEqual(json.load(bad_window.exception)["status"], "INVALID_ARGUMENT")

        key = {"tenant_id": "t1", "ip": "192.168.106.112"}
        asset = {"method": "AssetQueryByIp", **key, "response": {"aid": 1001, "managedLevel": 0}}
        get_asset = lambda port: self.stub.AssetQueryByIp(pb.AssetQueryByIpReq(**key, port=port), timeout=3)
        with post(asset, "/api/source") as reply:
            baseline = json.load(reply)
        self.assertEqual((baseline["count"], baseline["now_cursor"]), (0, "3"))
        self.assertEqual(get_asset(80).managedLevel, 0)
        asset["response"]["managedLevel"] = 1
        with self.assertRaises(HTTPError):
            post({**asset, "change": {"asset_id": 999}}, "/api/source")
        self.assertEqual(get_asset(80).managedLevel, 0)
        event = {"attrs": ["managed_level"], "macs": ["00:1A:2B:3C:4D:5E"], "dev_type": "STA",
                 "agent_id": "agent-001", "dev_id": "BBFA39A7",
                 "origin_devs": [{"dev_id": "od-7788", "dev_type": "ADAPTER"}], "changed_at_ms": 1763284600000}
        with post({**asset, "change": event}, "/api/source") as reply:
            window = json.load(reply)
        self.assertEqual(window, {"count": 1, "cursor": "3", "now_cursor": "4"})
        bounds = {"cursor": window["cursor"], "until_cursor": window["now_cursor"]}
        tenants = self.stub.ListChangedTenants(pb.ListChangedTenantsReq(**bounds)).tenants
        ids = [t.tenant_id for t in tenants]
        self.assertEqual(ids, ["t1"])
        change, = self.stub.ListAssetChanges(pb.ListAssetChangesReq(**bounds, tenant_ids=ids)).changes
        self.assertEqual((change.asset_id, list(change.ips), list(change.attrs)), (1001, [key["ip"]], ["managed_level"]))
        self.assertEqual((change.changed_at_ms, change.origin_devs[0].dev_id), (1763284600000, "od-7788"))
        self.assertEqual(list(change.macs), ["00:1a:2b:3c:4d:5e"])
        self.assertEqual((get_asset(80).managedLevel, get_asset(443).managedLevel), (1, 1))
        auth = {"method": "AuthQueryByIp", **key, "response": {"user_id": "user-01"}}
        with post(auth, "/api/source") as reply:
            self.assertEqual(json.load(reply)["now_cursor"], "4")
        for method, field, expected in [("AssetQueryByIp", "managedLevel", 1), ("AuthQueryByIp", "user_id", "user-01")]:
            with client.open(base + f"/api/preview?method={method}&tenant_id=t1&ip=192.168.106.112") as reply:
                self.assertEqual(json.load(reply)["response"][field], expected)
            with post(key, "/api/rpc/" + method) as reply:
                self.assertEqual(json.load(reply)["response"][field], expected)
        with post(bounds, "/api/rpc/ListChangedTenants") as reply:
            self.assertEqual(json.load(reply)["response"]["tenants"][0]["tenant_id"], "t1")
        with post({**bounds, "tenant_ids": ["t1"], "limit": 1}, "/api/rpc/ListAssetChanges") as reply:
            page = json.load(reply)["response"]
            self.assertEqual(page["changes"][0]["asset_id"], 1001)
            self.assertTrue(page["has_more"])
        for body in ({"unknown": 1}, [], {"limit": -1}):
            with self.assertRaises(HTTPError) as invalid_request:
                post(body, "/api/rpc/ListAssetChanges")
            self.assertEqual(invalid_request.exception.code, 400)
        with self.assertRaises(HTTPError) as missing_http:
            post({"tenant_id": "another", "ip": key["ip"]}, "/api/rpc/AssetQueryByIp")
        self.assertEqual(json.load(missing_http.exception)["status"], "NOT_FOUND")
        with self.assertRaises(grpc.RpcError) as missing:
            self.stub.AssetQueryByIp(pb.AssetQueryByIpReq(tenant_id="another", ip=key["ip"]))
        self.assertEqual(missing.exception.code(), grpc.StatusCode.NOT_FOUND)

    def test_zero_asset_compatibility(self):
        mock.write_changes(self.db, [{"tenant_id": "t1", "asset_id": 0, "kind": "UPDATE", "spread_sec": 120}])
        tenant, = self.stub.ListChangedTenants(pb.ListChangedTenantsReq()).tenants
        event, = self.stub.ListAssetChanges(pb.ListAssetChangesReq()).changes
        self.assertTrue(tenant.tenant_wide)
        self.assertEqual((tenant.spread_sec, event.kind, event.asset_id), (120, pb.UPDATE, 0))


if __name__ == "__main__":
    unittest.main()
