# JY Polymarket Copy CLI

交互式命令行工具，用于部署和管理 Polymarket JetFadil 第一版实时跟单机器人。

第一版采用稳健的混合方案：

- 低频调用 Polymarket Data API `/activity` 发现目标账号的新成交。
- 使用 CLOB API 获取订单簿参数并下单。
- 对 HTTP `429`、`5xx`、网络抖动做自动退避和重试。
- 对 `/book` 做短缓存，并默认启用 Polymarket Market WebSocket 维护近期 asset 的盘口参数，减少发现成交后的 HTTP 请求。
- 默认 `PRICE_MODE=safe`，按目标成交价加最大滑点下单，盘口超过保护价就跳过，避免 0.99/0.01 强制成交造成大滑点。
- 新增 `BOT_MODE=quant` AI量化模式，第一版支持 BTC 5分钟 Up/Down，使用 OKX 公共行情做动量试算。
- 默认 `DRY_RUN=1`，先只打印不真实下单。
- VPS 一键安装，安装后直接用 `jy` 打开交互菜单。

> 重要：不要把 `.env`、私钥、API Key 提交到 GitHub。

## VPS 一键安装

登录 VPS 后运行：

```bash
curl -fsSL https://raw.githubusercontent.com/leosysd/JY/main/scripts/install.sh | bash
```

安装完成后输入：

```bash
jy
```

然后在菜单里操作：

```text
1. 初始化/修改交易配置
2. 查看当前配置
3. 测试 API/私钥/签名
4. 安装/刷新 systemd 服务
5. 启动服务
6. 停止服务
7. 重启服务
8. 查看服务状态
9. 查看实时日志
10. 切换 DRY_RUN
11. 修改跟单比例 COPY_RATIO
12. 修改目标用户/钱包
13. 修改价格保护
14. 关闭开机自启
15. 切换策略模式 BOT_MODE
16. 修改 AI量化参数
17. AI量化单次试算
18. 更新程序
0. 退出
```

第一次使用建议顺序：

```text
1 -> 初始化/修改交易配置
3 -> 测试 API/私钥/签名
4 -> 安装/刷新 systemd 服务
9 -> 查看实时日志
```

确认日志正常后，再用菜单 `10` 把 `DRY_RUN` 从 `1` 改成 `0`。

## 私钥和 API

自动交易必须有你自己的 Polymarket 私钥。没有私钥只能监控或 `DRY_RUN`，不能真实下单。

菜单 `1. 初始化/修改交易配置` 会设置：

- `PRIVATE_KEY`: 你自己的交易钱包私钥，用来本地签名订单。
- `DEPOSIT_WALLET_ADDRESS`: 你的 Polymarket funder/API 地址，也就是 CLOB 下单时实际持有资金和仓位的钱包地址。
- `CLOB_API_URL`: 默认 `https://clob.polymarket.com`。
- `SIGNATURE_TYPE`: 新 API 用户通常用 `3`。
- `COPY_RATIO`: 跟单比例。
- `POLL_SEC`: 监听间隔。
- `DRY_RUN`: `1` 只打印，`0` 真实下单。
- `PRICE_MODE`: 默认 `safe`，使用目标成交价 + 最大滑点；`aggressive` 会恢复 0.99/0.01 强制成交，不建议实盘使用。
- `MAX_SLIPPAGE`: 默认 `0.02`，表示最多比目标成交价差 2 分。
- `MAX_ORDER_USDC`: 默认 `0` 不限制；大于 0 时限制单笔跟单最大名义金额。
- `BOT_MODE`: `copy` 为跟单模式，`quant` 为 AI量化模式。
- `QUANT_ORDER_USDC`: AI量化每次计划下单金额，默认 `5`。
- `QUANT_MIN_EDGE`: AI量化最小优势，默认 `0.04` 表示预测概率至少比买入价高 4 分。
- `QUANT_MIN_SECONDS_LEFT`: 距离 5分钟市场结束至少剩余多少秒才允许下单，默认 `45`。
- `TARGET_USERNAME` / `TARGET_WALLET`: 目标账号。
- `ENABLE_MARKET_WS`: 默认 `1`，启用 Market WebSocket 盘口缓存。
- `MARKET_WS_URL`: 默认 `wss://ws-subscriptions-clob.polymarket.com/ws/market`。

Polymarket CLOB 的 `apiKey / secret / passphrase` 会由 SDK 根据 `PRIVATE_KEY` 自动派生。你一般不需要手动填写 Relayer API Key。

如果 `jy test` 里看到 SDK 打印过 `Could not create api key`，但最后仍显示 `CLOB API 凭证可自动派生`，通常表示 SDK 创建新 key 的尝试返回提示，但已经成功派生或加载了可用凭证。

私钥只保存在 VPS 的：

```text
/opt/polymarket-copy/.env
```

安装器和菜单会把权限设置成：

```bash
chmod 600 /opt/polymarket-copy/.env
```

## 常用命令

打开交互菜单：

```bash
jy
```

测试配置：

```bash
jy test
```

查看服务：

```bash
jy service status
```

查看日志：

```bash
jy service logs
```

更新程序：

```bash
jy update
```

切换真实下单：

```bash
jy set-dry-run 0 --restart
```

切回只打印：

```bash
jy set-dry-run 1 --restart
```

设置价格保护：

```bash
jy set-price-protection --mode safe --max-slippage 0.02 --max-order-usdc 0 --restart
```

AI量化单次试算，不会真实下单：

```bash
jy quant-once
```

切换到 AI量化模式：

```bash
jy set-bot-mode quant --restart
```

修改 AI量化参数：

```bash
jy set-quant-config --order-usdc 5 --min-edge 0.04 --min-seconds-left 45 --restart
```

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

默认不开机自启。菜单 `14. 关闭开机自启` 或下面命令可关闭已有自启：

```bash
jy service disable-autostart
```

菜单 `18. 更新程序` 或 `jy update` 会执行：

- `git pull --ff-only`
- 更新 Python 依赖
- 重新安装本项目
- 重启 `polymarket-copy` 服务

`.env`、`seen_*.json`、`venv/` 都不会被 Git 覆盖。

## 第一版边界

这一版不做历史补仓、不做定时仓位拉平、不做自动撤单、不做完整盈亏统计。

AI量化第一版是动量/概率试算，不是收益保证；价格参考源使用 OKX 公共 BTC-USDT 行情，Polymarket 5分钟 BTC 市场实际规则以页面说明的数据源为准。

目标用户成交发现仍使用 Polymarket Data API 的低频 `/activity` 查询。Market WebSocket 用于维护已知 asset 的订单簿参数和盘口更新；如果 WS 断线或没有缓存，机器人会自动回退到 HTTP `/book`。任意目标钱包的纯链上 WebSocket 监听会作为后续高级模式。
