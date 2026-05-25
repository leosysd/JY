# JY Polymarket Copy CLI

交互式命令行工具，用于部署和管理 Polymarket JetFadil 第一版实时跟单机器人。

第一版采用稳健的混合方案：

- 低频调用 Polymarket Data API `/activity` 发现目标账号的新成交。
- 使用 CLOB API 获取订单簿参数并下单。
- 对 HTTP `429`、`5xx`、网络抖动做自动退避和重试。
- 对 `/book` 做短缓存，并默认启用 Polymarket Market WebSocket 维护近期 asset 的盘口参数，减少发现成交后的 HTTP 请求。
- 默认 `PRICE_MODE=safe`，按目标成交价加最大滑点下单，盘口超过保护价就跳过，避免 0.99/0.01 强制成交造成大滑点。
- 新增 `BOT_MODE=quant` AI量化模式，第一版支持 BTC 5分钟 Up/Down，默认使用 Polymarket RTDS 的 Chainlink BTC/USD 结算源做动量试算。
- 新增 `QUANT_STRATEGY=lock` 锁利模拟：固定 20 份、多次补单/反向单、300 USDC 单市场上限、两边成本和锁利记录。
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
18. 查看文件日志
19. 清空文件日志
20. 更新程序
21. 查看 AI量化数据统计
22. 查看 AI量化数据
23. 清空 AI量化数据
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
- `MARK_FAILED_SEEN`: 默认 `0`，跟单遇到网络/下单异常时不标记 seen，下轮继续尝试；改成 `1` 会失败后也标记 seen，降低重复下单风险但可能漏跟。
- `BOT_MODE`: `copy` 为跟单模式，`quant` 为 AI量化模式。
- `QUANT_PRICE_SOURCE`: 默认 `chainlink`，通过 Polymarket RTDS 订阅 Chainlink BTC/USD；也可手动改成 `okx` 做参考行情对比。
- `QUANT_CHAINLINK_SYMBOL`: 默认 `btc/usd`。
- `QUANT_CHAINLINK_WS_URL`: 默认 `wss://ws-live-data.polymarket.com`。
- `QUANT_CHAINLINK_MAX_AGE_SEC`: 默认 `180`，超过这个秒数仍无最新 Chainlink 价格才认为行情过旧。
- `QUANT_STRATEGY`: `single` 为单边信号模型，`lock` 为 JetFadil 风格锁利模拟模型。
- `QUANT_SIZE_MODE`: `usdc` 按金额换算份额，`shares` 按固定份额下单。锁利模拟建议 `shares`。
- `QUANT_ORDER_USDC`: AI量化每次计划下单金额，默认 `5`。
- `QUANT_ORDER_SHARES`: AI量化每次计划下单份额，默认 `20`，用于模拟 JetFadil 常见 20 份一笔。
- `QUANT_CAPITAL_USDC`: AI量化模拟本金，默认 `300`。
- `QUANT_MARKET_MAX_USDC`: 单个 5分钟市场最大模拟成本，默认 `300`。
- `QUANT_MAX_TRADES_PER_MARKET`: 单市场最多模拟笔数，默认 `35`。
- `QUANT_REBUY_COOLDOWN_SEC`: 补单/反手最短间隔，默认 `5` 秒。
- `QUANT_LOCK_MIN_PROFIT`: 两边都盈利多少 USDC 后视为锁利，默认 `0.50`。
- `QUANT_LOCK_STOP_ON_LOCK`: 默认 `1`，锁利后停止继续模拟该市场。
- `QUANT_MIN_EDGE`: AI量化最小优势，默认 `0.04`。现在按保护限价 `limit_price` 计算有效 edge，`best_ask` 只做参考记录。
- `QUANT_MIN_SECONDS_LEFT`: 距离 5分钟市场结束至少剩余多少秒才允许下单，默认 `45`。
- `QUANT_RECORD_SIGNALS`: 默认 `1`，AI量化运行时记录结构化信号数据。
- `QUANT_SIGNAL_FILE`: 默认 `data/quant_signals.jsonl`，一行一个 JSON 记录，方便 24 小时后复盘。
- `QUANT_SIGNAL_INTERVAL_SEC`: 默认 `30`，同类信号最短记录间隔。
- `LOG_TO_FILE`: 默认 `1`，机器人运行时同时写入文件日志，方便 AI 复盘。
- `LOG_FILE`: 默认 `logs/polymarket-copy.log`。
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

更新完成后服务会保持停止状态，需要运行时再手动启动：

```bash
jy service start
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
jy set-quant-config --price-source chainlink --order-usdc 5 --min-edge 0.04 --min-seconds-left 45 --restart
```

查看文件日志：

```bash
jy app-logs tail
```

清空文件日志：

```bash
jy app-logs clear
```

查看 AI量化结构化数据统计：

```bash
jy quant-data summary
```

查看 AI量化数据末尾记录：

```bash
jy quant-data tail
```

清空 AI量化数据：

```bash
jy quant-data clear
```

## 第一阶段：24小时模拟数据

第一阶段不真实下单，只跑 `BOT_MODE=quant` + `DRY_RUN=1`，机器人会把每轮 AI量化判断写入：

```text
/opt/polymarket-copy/data/quant_signals.jsonl
```

推荐开始前执行：

```bash
jy set-bot-mode quant
jy set-dry-run 1
jy set-quant-config --price-source chainlink --record-signals 1 --signal-interval-sec 30
jy quant-data clear
jy app-logs clear
jy service restart
```

跑 24 小时后查看：

```bash
jy quant-data summary
jy quant-data tail --lines 5
```

每条记录包含市场、剩余秒数、Chainlink BTC/USD 行情、Up/Down 概率、盘口 ask、edge、模拟选择方向和跳过原因。

## JetFadil 风格锁利模拟

这个模型只做 `DRY_RUN` 模拟，不会真实下单。它参考 JetFadil 近期公开成交的结构：常见为每笔 20 份，单个 5分钟市场内多次买入 Up/Down，两边都可能补单，目标是把最差结果逐步抬高，出现两边都盈利时停止该市场。

启用推荐命令：

```bash
jy set-bot-mode quant
jy set-dry-run 1
jy set-quant-config --strategy lock --size-mode shares --order-shares 20 --capital-usdc 300 --market-max-usdc 300 --max-trades-per-market 35 --rebuy-cooldown-sec 5 --lock-min-profit 0.50 --price-source chainlink --record-signals 1 --signal-interval-sec 5
jy quant-data clear
jy app-logs clear
jy service restart
```

锁利模型每轮会记录：

- 当前市场、剩余秒数、Chainlink 结算源价格。
- Up/Down 盘口 ask、概率、edge。
- 当前模拟仓位：Up 份额/成本、Down 份额/成本、总成本。
- 如果 Up 赢和如果 Down 赢分别赚亏多少。
- 模拟本金账本：已结算市场盈亏、未结算市场占用成本、当前可继续加仓金额。
- 候选补单是否能锁利、是否能改善最差亏损、是否只是方向优势补单。

查看效果：

```bash
jy quant-data summary
jy quant-data tail --lines 10
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

菜单 `20. 更新程序` 或 `jy update` 会执行：

- `git pull --ff-only`
- 更新 Python 依赖
- 重新安装本项目
- 停止 `polymarket-copy` 服务

`.env`、`seen_*.json`、`venv/` 都不会被 Git 覆盖。

## 第一版边界

这一版真实下单仍不做历史补仓、不做定时仓位拉平、不做自动撤单、不做完整盈亏统计。`QUANT_STRATEGY=lock` 只做模拟仓位、补单、反向单和锁利记录。

AI量化第一版是动量/概率试算，不是收益保证；默认价格源使用 Polymarket RTDS 的 Chainlink BTC/USD，与 Polymarket 5分钟 BTC 市场页面规则里的结算源保持一致。OKX 只作为可选参考行情源。

目标用户成交发现仍使用 Polymarket Data API 的低频 `/activity` 查询。Market WebSocket 用于维护已知 asset 的订单簿参数和盘口更新；如果 WS 断线或没有缓存，机器人会自动回退到 HTTP `/book`。任意目标钱包的纯链上 WebSocket 监听会作为后续高级模式。

## AI量化路线

1. 第一阶段：单边 AI量化，先记录信号。
   - `BOT_MODE=quant` + `DRY_RUN=1`。
   - 每个 5分钟市场最多模拟一次买入，不加单、不反向下单。
   - 记录 Chainlink 价格、开盘价、剩余秒数、盘口 ask、edge、模拟方向和跳过原因。
   - 目标是先跑 24 小时，确认信号质量和数据完整性。

2. 第二阶段：仓位记录 + 两边成本计算。已在 `QUANT_STRATEGY=lock` 模拟模型中加入。
   - 按市场记录我们自己的 Up/Down 持仓、均价、成本、已模拟成交。
   - 计算如果 Up 赢的净收益、如果 Down 赢的净收益。
   - 先只模拟，不真实下单。

3. 第三阶段：补单模块。已在 `QUANT_STRATEGY=lock` 模拟模型中加入。
   - 同方向价格更好、edge 仍然达标时，允许小额补单。
   - 设置单市场最大成本、最大补单次数、最晚补单剩余秒数。
   - 避免因为连续补单把风险放大。

4. 第四阶段：反向单模块。已在 `QUANT_STRATEGY=lock` 模拟模型中加入。
   - 当另一边价格足够低时，允许买入反边降低单边风险。
   - 反向单数量由当前两边成本和目标风险决定，不盲目对冲。
   - 用模拟数据观察是否接近 JetFadil 的补单/反手结构。

5. 第五阶段：锁利模块。已在 `QUANT_STRATEGY=lock` 模拟模型中加入。
   - 同时计算 Up 赢和 Down 赢时的净收益。
   - 如果两边结果都大于设定利润阈值，就停止交易这个市场。
   - 如果只能降低亏损但不能锁利，则按风险参数决定是否继续。

6. 第六阶段：实盘小金额跑。
   - 只在前面阶段模拟稳定后开启。
   - `DRY_RUN=0`，但限制 `QUANT_ORDER_USDC`、单市场最大成本、每日最大亏损。
   - 先小金额验证执行、滑点、成交率和真实盈亏。
