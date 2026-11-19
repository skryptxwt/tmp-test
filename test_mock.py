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
        self.assertEqual(list(summary.tenants[0].attrs), ["assetIp", "managedLevel"])
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
        http = mock.make_console(0, self.stub, "test", lambda rows: mock.write_changes(self.db, rows))
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(http.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(http.shutdown)
        base = f"http://127.0.0.1:{http.server_port}"
        client = build_opener(ProxyHandler({}))

        def post(rows):
            return client.open(Request(base + "/api/changes", json.dumps(rows).encode(),
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


if __name__ == "__main__":
    unittest.main()
