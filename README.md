# tmp-test · IdtProxy 资产变更 mock

使用随附的原始 `idt_proxy.proto` 和生成的 Python 存根。实现的两个 unary RPC 为 `/IdtProxy/ListChangedTenants` 和 `/IdtProxy/ListAssetChanges`。原协议中的其他三个 RPC 返回 gRPC `UNIMPLEMENTED`。

## Docker 镜像

GitHub Actions 在推送到 `main` 时自动构建 Linux amd64 镜像，并在容器内运行集成测试和启动检查。镜像名为 `lengdanlexin/tmp-test`。

在 GitHub 仓库的 **Settings → Secrets and variables → Actions → New repository secret** 添加 `DOCKERHUB_TOKEN`，值为 Docker Hub 账号 `lengdanlexin` 的可写访问令牌。配置前工作流仍会构建和测试；配置后在 **Actions → Build and publish Docker image → Run workflow** 选择 `main` 运行，或推送新提交。发布标签为 `latest` 和完整 Git 提交 SHA。

发布后可以这样启动镜像，使用命名卷保存变更日志。

```sh
docker run -d --name idt-proxy-mock -p 127.0.0.1:8765:8765 -p 127.0.0.1:50051:50051 -v idt-proxy-data:/data lengdanlexin/tmp-test:latest
```

打开 [配置与接口测试页](http://127.0.0.1:8765/)，gRPC 客户端连接 `127.0.0.1:50051`。

## 本地启动与追加变更

在本目录打开 PowerShell，安装依赖并启动服务。默认使用明文 gRPC，监听 `127.0.0.1:50051`。

```powershell
$env:http_proxy = 'http://127.0.0.1:7897'
$env:https_proxy = $env:http_proxy
python -m pip install -r requirements.txt
python mock.py serve
```

### 浏览器配置

打开 [本机配置页](http://127.0.0.1:8765/)，选择 **表单配置**，填写租户 ID、变更类型和资产 ID 后点击 **提交变更**。选择 **整租户 · TENANT** 可以设置分摊窗口。需要一次提交多条变更或指定原始设备列表等字段时，选择 **批量 JSON**；两个模式都可通过 **填入示例** 查看格式。

提交后，右侧直接调用 gRPC 查询本次写入的游标区间。在 **gRPC 接口测试** 选择 **资产明细** 或 **租户汇总**，填写起止游标、每页条数和适用的租户过滤条件，点击 **调用接口** 查看请求 JSON、实际响应和调用耗时。**查询最新数据** 清空起止游标，保留租户过滤与分页设置，查询截至点击时刻的日志；**下一页** 使用上一页返回的游标继续读取。每次提交均追加新变更，并保存在当前服务使用的数据库中。

直接运行 Python 时，配置页默认监听本机回环地址。可用 `python mock.py serve --web-port 8766` 切换页面端口，或用 `--web-port 0` 仅启动 gRPC。

### 命令行追加

在同一目录打开第二个终端，把示例写入运行中的服务。

```powershell
python mock.py append changes.example.json
```

编辑 JSON 中的 `tenant_id`、`asset_id`、`kind` 和 `attrs` 就能指定变化的租户、资产和属性。示例向 tenant-a 写入资产 1001 的 UPDATE、资产 1002 的 INSERT，向 tenant-b 写入分摊时间 120 秒的整租户变更。模拟删除时填 `"kind": "DELETE"` 和待失效的资产 ID。IP、MAC 等字段按协议填变化前后用于匹配的值。

每次 append 都将整个数组作为一批新事件提交，文件顺序就是序号顺序。省略 `changed_at_ms` 时自动填写当前毫秒时间戳。默认的 `changes.sqlite3` 保留重启前的日志；可在两条命令的子命令前传入相同的 `--db 路径` 来切换场景。

## 用原客户端或 grpcurl 查询

将已有客户端的 gRPC 地址指向 `127.0.0.1:50051`，也可以在 PowerShell 中调用 grpcurl。显式指定原 proto 即可解析服务。

```powershell
grpcurl -plaintext -import-path . -proto idt_proxy.proto -d '{}' 127.0.0.1:50051 IdtProxy/ListChangedTenants
'{"cursor":"0","untilCursor":"3","tenantIds":["tenant-a"]}' | grpcurl -plaintext -import-path . -proto idt_proxy.proto -d '@' 127.0.0.1:50051 IdtProxy/ListAssetChanges
```

在空库追加一次示例后，第一个查询返回 tenant-a 和 tenant-b，其中 tenant-b 的 `tenant_wide` 为 true。第二个查询按序返回 tenant-a 的 1001、1002。示例中的 `3` 是这批数据的结束游标，实际轮询使用响应返回的游标。

## 游标和分页

- mock 将空 `cursor` 解释为 0，空 `until_cursor` 取请求开始时的最新序号。两个接口共享同一序号空间。
- 同一轮的查询固定结束游标。第一次以空终点发起请求时，用第一次响应的 `now_cursor` 固定后续页的 `until_cursor`。每页用 `next_cursor` 推进起点，直到 `has_more=false`。查询对应的资产明细时，从原窗口起点读起。
- mock 默认每页 100 项，最大 1000 项，最多扫描 2000 条日志。达到项数或扫描上限会返回 `has_more=true`，包括刚好读到终点的情况。租户汇总按本页扫描区间合并属性，同一租户可以出现在多页。明细过滤也会推进扫描游标，空页仍需检查 `has_more`。
- 要验证日志缺口，可使用独立测试库执行 `python mock.py --db gap.sqlite3 append changes.example.json --keep-last 1`，再用该库启动服务。此时查询 `(0,3]` 会返回 `gap=true` 和 `now_cursor="3"`。

运行 `python -m unittest -v test_mock` 可验证实际 gRPC 调用、追加后的可见性、分页、租户过滤和日志缺口。
