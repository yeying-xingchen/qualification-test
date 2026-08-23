# GCash 资格检测 GUI

独立的批量 GCash qualification checker，不依赖其他项目目录。

Sentinel 运行时需要 Node.js 18+ 和本地 `jsdom` 依赖；首次安装请执行 `npm install`。

## 功能

- 使用 PH/PHP Checkout 检测是否发布 GCash 渠道
- 输出 Checkout 返回的全部可用支付渠道，并单独标记 `gcash_available`
- GCash 资格只认定 `cpmt_1TOgstC6h1nxGoI3WUVEY2cJ`，其他支付方式不会判定为 GCash
- GUI 内置预设：菲律宾 GCash、菲律宾 Card、英国 PayPal、荷兰 iDEAL、越南 MoMo
- 支持批量 Token 和代理
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
