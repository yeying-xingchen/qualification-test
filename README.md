# 支付渠道资格检测 GUI

独立的批量 Checkout 支付渠道资格检测工具，不依赖其他项目目录。

Sentinel 运行时需要 Node.js 18+ 和本地 `jsdom` 依赖；首次安装请执行 `npm install`。

## 功能

- 按国家和币种创建 Checkout，检测是否发布所选支付渠道
- 支持**同时勾选多个地区**（如菲律宾 GCash + 英国 PayPal + 荷兰 iDEAL），每个地区独立创建对应国家/币种的 Checkout
- **地区与渠道合并**：每个地区预设即一个「地区 + 渠道」组合，勾选哪个地区就检测该渠道，无需再单独选择渠道
- 输出全部地区 Checkout 返回的全部可用支付渠道（按地区标注），并分别标记每个地区目标渠道是否可用
- GCash 资格只认定 `cpmt_1TOgstC6h1nxGoI3WUVEY2cJ`，其他支付方式不会判定为 GCash
- GUI 内置预设：菲律宾 GCash、菲律宾 Card、英国 PayPal、荷兰 iDEAL、越南 MoMo、印度尼西亚 GoPay、印度 UPI、波兰 BLIK、巴西 PIX
- 支持批量 Token 和代理
- 支持按渠道单独配置代理，未配置渠道自动回退到通用代理池
- 支持原始 JWT，以及 `email----...----JWT` 整行账号格式
- 支持 `host:port:user:password`、`curl -x/-U`、HTTP/HTTPS、SOCKS5/SOCKS5H 代理
- 代理池轮询复用；代理隧道失败时自动尝试备用入口
- 只读取 Checkout 支付方式，不调用 confirm/start，不发起实际支付；波兰 BLIK 使用波兰出口代理

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

支持预设：`gcash`、`card`、`paypal_uk`、`ideal_nl`、`momo_vn`、`gopay_id`、`upi_in`、`blik_pl`、`pix_br`。波兰 BLIK 使用 `PL` / `PLN` 创建 Checkout，巴西 PIX 使用 `BR` / `BRL` 创建 Checkout；响应中的 `channel_details` 会包含 OpenAI Checkout 返回的渠道名称、ID 和原始类型。

批量检测仍使用 `POST /api/gcash/batch`，任务状态使用 `GET /api/gcash/batch/<job_id>`。除传统 `proxies` 外，也可传入按渠道配置的 `channel_proxies`：

```json
{"tokens":["<JWT>"],"proxies":["fallback:8080"],"channel_proxies":{"gcash":["ph-gcash:8080"],"card":"ph-card:8080"},"target_channel":"gcash"}
```

渠道代理优先于通用代理；每个渠道支持字符串、数组或换行字符串。

**多地区同时检测**：请求体传 `regions` 数组，每个元素是一个预设名或对象（`name` 用于结果展示，`preset` 指定内置预设；对象内可覆盖 `channel`、`country`、`currency`、`plan`）。地区与渠道已合并：每个预设本身就是一个「地区 + 渠道」组合（渠道即该地区 Checkout 的目标支付方式），每个地区独立创建该国家/币种的 Checkout，并使用该渠道（`channel_proxies[channel]` 或通用代理池）的代理：

```json
{
  "tokens": ["<JWT>"],
  "proxies": ["fallback:8080"],
  "channel_proxies": {"gcash": ["ph-gcash:8080"], "paypal": ["uk-paypal:8080"]},
  "regions": [
    {"name": "菲律宾·GCash", "preset": "gcash"},
    {"name": "英国·PayPal", "preset": "paypal_uk"},
    {"name": "自定义·PIX", "preset": "custom", "channel": "pix", "country": "BR", "currency": "BRL"}
  ],
  "workers": 8
}
```

`regions` 也支持 `{"gcash": {...}, "paypal_uk": {...}}` 字典形式。未传 `regions` 时保持原有单预设行为。每个结果行包含 `regions` 数组（各地区 `qualified`、`available_channels`、`checkout_session_id`、`evidence` 或 `error`），同时汇总 `available_channels`（全部地区的渠道并集）与 `channel_details`；只要任一地区目标渠道可用即视为有资格。地区失败不影响其他地区的结果。

## 有资格 Token 批量操作

批量检测完成后，结果区域提供两个操作：

- **复制全部有资格 Token**：提取检测结果中至少有一个已选渠道可用（`ok=true` 且任意已选渠道可用）的 Access Token，并按每行一个 Token 的格式复制到剪贴板。
- **提交全部有资格 Token**：填写目标 API 地址后，手动点击提交按钮，将全部有资格 Token 通过 `POST` 请求发送到目标 API。系统不会在检测完成后自动提交。

目标 API 请求格式：

```http
POST https://example.com/api/tokens
Content-Type: application/json
```

```json
{
  "tokens": ["<qualified-token-1>", "<qualified-token-2>"],
  "count": 2
}
```

目标 API 返回 HTTP 2xx 时，页面显示提交成功；非 2xx 响应或网络错误会显示失败原因。由于 Token 属于敏感凭据，请仅提交到受信任的 HTTPS 地址，并避免将 Token 写入日志、截图或公共工单。

## 代理示例

```text
us.1024proxy.io:3000:USERNAME:PASSWORD
curl -x us.1024proxy.io:3000 -U "USERNAME:PASSWORD" ipinfo.io
http://USERNAME:PASSWORD@us.1024proxy.io:3000
socks5://USERNAME:PASSWORD@host:1080
```

## 环境变量

- `GCASH_WORKERS`：每个批次默认并发检测数，默认 4
- `GCASH_MAX_WORKERS`：单进程允许的每批最大并发数，默认 32
- `HOST`、`PORT`：监听地址，默认 `127.0.0.1:18097`

批量接口可在请求体中传入 `workers` 覆盖本批次并发数，例如：

```json
{"tokens":["<JWT-1>","<JWT-2>"],"proxies":["proxy:8080"],"workers":16}
```

`workers` 必须在 `1` 到 `GCASH_MAX_WORKERS` 之间；任务提交后立即返回 `job_id`，通过批量状态接口轮询结果。
