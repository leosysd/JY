# JY Polymarket Copy CLI

交互式命令行工具，用于部署和管理 Polymarket JetFadil 第一版实时跟单机器人。

第一版采用稳健的混合方案：

- 低频调用 Polymarket Data API `/activity` 发现目标账号的新成交。
- 使用 CLOB API 获取订单簿参数并下单。
- 对 HTTP `429`、`5xx`、网络抖动做自动退避和重试。
- 对 `/book` 做短缓存，减少重复请求。
- 默认 `DRY_RUN=1`，先只打印不真实下单。
- 通过 SSH/SCP 部署到 VPS，并用 systemd 守护运行。

> 重要：不要把 `.env`、私钥、API Key 提交到 GitHub。

## 本地使用

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -e .
jy-cli
```

也可以直接运行：

```powershell
python -m polymarket_copy.cli
```

## 常用命令

初始化本地配置：

```powershell
jy-cli init-config
```

校验配置：

```powershell
jy-cli validate
```

本地运行机器人：

```powershell
jy-cli run
```

部署到 VPS：

```powershell
jy-cli deploy --host 你的VPS_IP --user root
```

查看远程状态：

```powershell
jy-cli remote status --host 你的VPS_IP --user root
```

查看远程实时日志：

```powershell
jy-cli remote logs --host 你的VPS_IP --user root
```

切换真实下单：

```powershell
jy-cli remote dry-run --host 你的VPS_IP --user root --value 0
```

## 配置项

复制 `.env.example` 为 `.env`，或者用 `jy-cli init-config` 交互生成。

关键字段：

- `TARGET_USERNAME`: 目标用户名，默认 `jetfadil`。
- `TARGET_WALLET`: 目标钱包，默认 JetFadil 文档里的地址。
- `PRIVATE_KEY`: 你自己的交易钱包私钥。
- `DEPOSIT_WALLET_ADDRESS`: 你自己的 Polymarket Deposit Wallet 地址。
- `COPY_RATIO`: 跟单比例，`1.0` 表示同数量跟单。
- `POLL_SEC`: 轮询间隔，默认 `1`。
- `DRY_RUN`: `1` 只打印，`0` 真实下单。

## VPS 部署结果

默认部署到：

```text
/opt/polymarket-copy/
```

systemd 服务名：

```text
polymarket-copy
```

常用远程命令：

```bash
systemctl status polymarket-copy
journalctl -u polymarket-copy -f
systemctl restart polymarket-copy
```

## 第一版边界

这一版不做历史补仓、不做定时仓位拉平、不做自动撤单、不做盈亏统计。

目标用户成交发现仍使用 Polymarket Data API 的低频 `/activity` 查询。Polymarket 官方 WebSocket 更适合盘口和自己账户通道；任意目标钱包的纯链上 WebSocket 监听会作为后续高级模式。
