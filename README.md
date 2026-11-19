# tmp-test · IdtProxy 资产变更 mock

使用原始 `idt_proxy.proto` 提供两个通知接口 `ListChangedTenants`、`ListAssetChanges` 和两个回源接口 `AssetQueryByIp`、`AuthQueryByIp`。回源响应和变更日志保存在同一个 SQLite 数据库。

## 验证托管状态从 0 刷新为 1

1. 打开页面的 **回源配置与测试**。保留示例租户 `t1`、IP `192.168.106.112` 和资产响应 `{"aid":1001,"managedLevel":0}`，点击 **仅保存回源值**。这一步用于准备初始值，变更游标保持原位。
2. 将实际数据湖测试实例的 IDT 客户端指向本 mock，启用 `assetChange.enable` 并设置 `pollSec=10`。用同一租户和 IP 的日志触发查询，确认数据湖读到 0。页面示例通知里的 MAC、设备 ID、类型、Agent ID 和原始设备应与这条日志的匹配身份一致。
3. 把回源响应里的 `managedLevel` 改为 1，通知里的 `attrs` 填 `["managed_level"]`。点击 **保存并通知**，新响应和通知在同一事务生效。通知时间默认取当前毫秒数，测试时钟偏差时可显式填写 `changed_at_ms`。按 proto 规则，返回的 MAC 为小写。
4. 在接口测试区核对本次游标区间的租户及变更明细。在数据湖消费到这条通知后，再发送同一条日志，确认数据湖在原 30 分钟 TTL 内读到 1。页面的 **查询当前回源值** 可独立核对回源接口。取数时间、标记保留、XFF 拆分和缓存命中情况由实际数据湖验证。

回源按 **接口名、租户、查询 IP 原文** 匹配；同一资产的不同端口和域名共享配置。未配置的键返回 `NOT_FOUND`。选择 `AuthQueryByIp` 可保存相同 IP 的独立认证响应。通知中未填的租户和 IP 从回源键补齐；普通资产通知可从资产响应的 `aid` 补齐 `asset_id`。独立的通知表单也可提交显式 `asset_id=0` 的 UPDATE 以测试整租户兼容分支，明细保留原 kind。

## 后台 HTTP API

网页和脚本使用同一组 JSON 接口。Docker 使用 host 网络时，在物理机本机访问 `http://127.0.0.1:8765`，其他机器用物理机 IP 替换回环地址。所有下列接口都使用 POST 和 `Content-Type: application/json`。

| 路径 | 对应网页操作 | 请求体 |
|---|---|---|
| `/api/changes` | 提交单条或批量变更 | `AssetChange` 数组；一条也放进数组 |
| `/api/source` | 仅保存回源值，或保存并通知 | `method`、`tenant_id`、`ip`、`response`；附上 `change` 则在同一事务中追加通知 |
| `/api/rpc/ListChangedTenants` | 查询变更租户 | `cursor`、`until_cursor`、`limit`，均可省略 |
| `/api/rpc/ListAssetChanges` | 查询变更明细 | 相同游标及分页参数，可附加 `tenant_ids` 数组 |
| `/api/rpc/AssetQueryByIp` | 查询当前资产回源值 | `AssetQueryByIpReq` 对象，按租户和 IP 查找 |
| `/api/rpc/AuthQueryByIp` | 查询当前认证回源值 | `AuthQueryByIpReq` 对象，按租户和 IP 查找 |

下列示例可直接在运行 mock 的 Linux 物理机上执行。

```sh
base=http://127.0.0.1:8765
post() { curl -sS -H 'Content-Type: application/json' --data-binary "$2" "$base$1"; echo; }

# 准备初始值，不追加通知
post /api/source '{"method":"AssetQueryByIp","tenant_id":"t1","ip":"192.168.106.112","response":{"aid":1001,"managedLevel":0}}'
# 改为托管并追加一条通知
post /api/source '{"method":"AssetQueryByIp","tenant_id":"t1","ip":"192.168.106.112","response":{"aid":1001,"managedLevel":1},"change":{"attrs":["managed_level"]}}'
# 查询当前回源值、变更租户、租户内的资产变更
post /api/rpc/AssetQueryByIp '{"tenant_id":"t1","ip":"192.168.106.112"}'
post /api/rpc/ListChangedTenants '{}'
post /api/rpc/ListAssetChanges '{"tenant_ids":["t1"],"limit":20}'
# 单独追加整租户通知
post /api/changes '[{"tenant_id":"t2","kind":"TENANT","attrs":["managed_level"],"spread_sec":120}]'
# 配置并读取认证响应
post /api/source '{"method":"AuthQueryByIp","tenant_id":"t1","ip":"192.168.106.112","response":{"user_id":"user-01"}}'
post /api/rpc/AuthQueryByIp '{"tenant_id":"t1","ip":"192.168.106.112"}'
```

写入成功返回 HTTP 201 和 `count`、`cursor`、`now_cursor`。要查看刚写入的一批，用返回的 `cursor` 作起点、`now_cursor` 作 `until_cursor`。查询成功返回 HTTP 200，JSON 的 `response` 为实际 gRPC 响应。参数有误返回 400，未配置回源值返回 404。翻页时保留终点和过滤条件，以 `next_cursor` 作下一页起点。

## Docker 镜像

GitHub Actions 在推送到 `main` 时自动构建 Linux amd64 镜像，并在容器内运行集成测试和启动检查。镜像名为 `lengdanlexin/tmp-test`。

在 GitHub 仓库的 **Settings → Secrets and variables → Actions → New repository secret** 添加 `DOCKERHUB_TOKEN`，值为 Docker Hub 账号 `lengdanlexin` 的可写访问令牌。配置前工作流仍会构建和测试；配置后在 **Actions → Build and publish Docker image → Run workflow** 选择 `main` 运行，或推送新提交。发布标签为 `latest` 和完整 Git 提交 SHA。

发布后可以这样启动镜像，使用命名卷保存变更日志。

```sh
docker run -d --name idt-proxy-mock -p 127.0.0.1:8765:8765 -p 127.0.0.1:50051:50051 -v idt-proxy-data:/data lengdanlexin/tmp-test:latest
```

打开 [配置与接口测试页](http://127.0.0.1:8765/)，gRPC 客户端连接 `127.0.0.1:50051`。

### 物理机网络下启用单向 TLS

给容器提供 PEM 格式的**服务端证书链和私钥**，原 gRPC 端口 `50051` 就会改用 TLS。客户端只需建立 TLS 连接，mock 不请求客户端证书。未设置 TLS 文件的实例沿用明文配置。配置页和后台 HTTP API 使用 `http://物理机IP:8765`，页面在服务端通过 TLS 调用 gRPC。

下面使用自签证书启动一个 Linux 测试实例。将 `HOST_IP` 改为客户端实际连接的物理机地址，在独立目录中执行一次生成命令；已有证书时直接挂载已有文件。

```sh
HOST_IP=192.0.2.10
mkdir -p tls
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -subj '/CN=idt-proxy' -addext "subjectAltName=DNS:idt-proxy,IP:127.0.0.1,IP:$HOST_IP" \
  -keyout tls/server.key -out tls/server.crt
sudo chgrp 10001 tls/server.key
chmod 640 tls/server.key
docker run -d --name idt-proxy-tls --pull always --network host --restart unless-stopped \
  -v idt-proxy-data:/data -v "$PWD/tls:/tls:ro" \
  -e IDT_TLS_CERT=/tls/server.crt -e IDT_TLS_KEY=/tls/server.key \
  lengdanlexin/tmp-test:latest
```

启动前请确认 `50051` 和 `8765` 可用；旧 mock 正在占用这两个端口时，先停止旧实例。挂载目录须允许容器 UID/GID `10001:10001` 遍历并读取证书和私钥。

在包含 `idt_proxy.proto` 的目录中调用。下例只向客户端提供用于验证服务端的证书：

```sh
grpcurl -cacert tls/server.crt -import-path . -proto idt_proxy.proto -d '{}' \
  "$HOST_IP:50051" IdtProxy/ListChangedTenants
```

若联调客户端也要跳过服务端证书校验，grpcurl 可将 `-cacert tls/server.crt` 换为 `-insecure`，请求仍使用 TLS。外部客户端的校验方式由各自的客户端配置决定。

使用内部 CA 时，服务端证书文件按叶证书、中间证书顺序拼接。另挂载 CA 根证书并设置 `IDT_WEB_GRPC_CA=/tls/ca.crt`，供网页后台验证 gRPC 服务。如果证书仅包含业务域名，可设置 `IDT_WEB_GRPC_NAME=证书中的域名`，网页后台仍通过本机地址连接。直接运行 Python 时，对应参数是 `serve --tls-cert ... --tls-key ...`、`--web-grpc-ca` 和 `--web-grpc-name`。

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
