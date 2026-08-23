# GCash 资格检测 GUI

独立的批量 GCash qualification checker，不依赖其他项目目录。

Sentinel 运行时需要 Node.js 18+ 和本地 `jsdom` 依赖；首次安装请执行 `npm install`。

## 功能

- 使用 PH/PHP Checkout 检测是否发布 GCash 渠道
- 输出 Checkout 返回的全部可用支付渠道，并单独标记 `gcash_available`
- GCash 资格只认定 `cpmt_1TOgstC6h1nxGoI3WUVEY2cJ`，其他支付方式不会判定为 GCash
- GUI 内置预设：菲律宾 GCash、菲律宾 Card、英国 PayPal、荷兰 iDEAL、越南 MoMo
- 支持批量 Token 和代理
- 支持按渠道单独配置代理，未配置渠道自动回退到通用代理池
- 支持原始 JWT，以及 `email----...----JWT` 整行账号格式
- 支持 `host:port:user:password`、`curl -x/-U`、HTTP/HTTPS、SOCKS5/SOCKS5H 代理
- 代理池轮询复用；代理隧道失败时自动尝试备用入口
- 只读取 Checkout 支付方式，不调用 confirm/start，不发起实际支付

## 启动

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
npm install
.venv/bin/python app.py
```

浏览器打开 `http://127.0.0.1:18097/`。

## 自部署与访客模式

默认是自部署模式，不需要 CDK，适合管理员直接运行。管理员可通过主页的“管理员后台”按钮进入独立管理页面，登录后切换全局模式；主页用户不能自行切换模式。访客模式需要有效 CDK，并按批次账号数量扣减额度；访客结果不会返回邮箱和 Access Token。

设置管理员密码：

```bash
export GCASH_ADMIN_PASSWORD='change-me'
export GCASH_SESSION_SECRET='replace-with-a-random-secret'
```

管理员 API：`POST /api/admin/login`、`GET /api/admin/status`、`POST /api/admin/mode`，其中 mode 为 `self` 或 `visitor`。

生成 CDK（设置 `GCASH_ADMIN_KEY` 后建议使用请求头鉴权）：

```bash
curl -X POST http://127.0.0.1:18097/api/cdk/create \
  -H 'Content-Type: application/json' -H 'X-Admin-Key: your-admin-key' \
  -d '{"quota":100,"days":30}'
```

校验 CDK：`POST /api/cdk/redeem`，请求体 `{\"cdk\":\"GC-...\"}`。

## API

列出内置预设：

```http
GET /api/presets
```

单账号检测并返回渠道 ID：

```http
POST /api/gcash/check
Content-Type: application/json
```

```json
{
  "token": "<JWT 或整行账号>",
  "proxy": "host:port:user:password",
  "preset": "paypal_uk"
}
```

支持预设：`gcash`、`card`、`paypal_uk`、`ideal_nl`、`momo_vn`。响应中的 `channel_details` 会包含 OpenAI Checkout 返回的渠道名称、ID 和原始类型。

批量检测仍使用 `POST /api/gcash/batch`，任务状态使用 `GET /api/gcash/batch/<job_id>`。除传统 `proxies` 外，也可传入按渠道配置的 `channel_proxies`：

```json
{"tokens":["<JWT>"],"proxies":["fallback:8080"],"channel_proxies":{"gcash":["ph-gcash:8080"],"card":"ph-card:8080"},"target_channel":"gcash"}
```

渠道代理优先于通用代理；每个渠道支持字符串、数组或换行字符串。

## 代理示例

```text
us.1024proxy.io:3000:USERNAME:PASSWORD
curl -x us.1024proxy.io:3000 -U "USERNAME:PASSWORD" ipinfo.io
http://USERNAME:PASSWORD@us.1024proxy.io:3000
socks5://USERNAME:PASSWORD@host:1080
```

## 环境变量

- `GCASH_MAX_BATCH`：单批最大数量，默认 100
- `GCASH_WORKERS`：并发检测数，默认 4
- `HOST`、`PORT`：监听地址，默认 `127.0.0.1:18097`
