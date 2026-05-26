# JY Polymarket Copy CLI

JY 是一个在 VPS 上运行的交互式命令行工具，用来管理 Polymarket 跟单和 AI 量化模拟。

当前主要功能：

- `copy`：跟单模式，监听目标账号公开成交，并按你的配置复制下单。
- `quant`：AI量化模拟模式，默认只记录信号，不真实下单。
- `jy`：交互式菜单，配置、测试、启动、停止、看日志、更新都在这里操作。

> 不要把 `.env`、私钥、API Key 提交到 GitHub。真实下单前请先用 `DRY_RUN=1` 跑一段时间。

## 第一次安装

登录 VPS 后执行：

```bash
curl -fsSL https://raw.githubusercontent.com/leosysd/JY/main/scripts/install.sh | bash
```

如果提示 `curl: command not found`，先安装 curl：

```bash
apt-get update && apt-get install -y curl
```

安装完成后输入：

```bash
jy
```

第一次建议按这个顺序操作：

```text
1  初始化/修改交易配置
3  测试 API/私钥/签名
4  安装/刷新 systemd 服务
5  启动服务
9  查看实时日志
```

如果只是先跑模拟或检查日志，`DRY_RUN` 保持 `1`。确认没问题后，再切到真实下单。

## 第一次配置怎么填

菜单 `1. 初始化/修改交易配置` 会逐项询问。方括号里的值是默认值，直接回车就是保留默认。

建议这样填：

```text
BOT_MODE:
  copy  = 跟单
  quant = AI量化模拟

TARGET_USERNAME:
  默认 jetfadil，可以直接回车

TARGET_WALLET:
  默认 JetFadil 钱包，可以直接回车

PRIVATE_KEY:
  你的交易钱包私钥
  只跑 DRY_RUN 可以先不填
  真实下单必须填

CHAIN_ID [137]:
  直接回车

CLOB_API_URL [https://clob.polymarket.com]:
  直接回车

启用 Market WebSocket 盘口缓存吗 [Y/n]:
  直接回车，默认启用

MARKET_WS_URL [wss://ws-subscriptions-clob.polymarket.com/ws/market]:
  直接回车

SIGNATURE_TYPE [3]:
  直接回车

DEPOSIT_WALLET_ADDRESS:
  填 Polymarket 个人资料里的 0x... 地址
  不要填充值页面的 TRON/USDC 充值地址

DRY_RUN:
  1 = 只打印，不真实下单
  0 = 真实下单
  第一次建议填 1
```

Relayer API 密钥一般不用手动填。程序会通过 `PRIVATE_KEY` 派生/加载 CLOB API 凭证。

## 常用命令

打开菜单：

```bash
jy
```

查看服务状态：

```bash
jy service status
```

查看实时日志：

```bash
jy service logs
```

启动服务：

```bash
jy service start
```

停止服务：

```bash
jy service stop
```

重启服务：

```bash
jy service restart
```

测试配置：

```bash
jy test
```

清空应用日志：

```bash
jy app-logs clear
```

查看 AI量化数据统计：

```bash
jy quant-data summary
```

清空 AI量化数据：

```bash
jy quant-data clear
```

## 更新程序

在 VPS 上执行：

```bash
cd /opt/polymarket-copy
jy update
```

更新完成后服务会保持停止状态。需要继续运行时手动启动：

```bash
jy service start
```

更新不会覆盖这些文件：

```text
/opt/polymarket-copy/.env
/opt/polymarket-copy/seen_*.json
/opt/polymarket-copy/data/
/opt/polymarket-copy/logs/
```

## 跟单模式

切到跟单：

```bash
jy set-bot-mode copy
```

先只打印：

```bash
jy set-dry-run 1 --restart
```

确认配置和日志正常后，切到真实下单：

```bash
jy set-dry-run 0 --restart
```

当前 copy 模式按无脑跟单处理：

```bash
jy set-price-protection --mode aggressive --max-slippage 0.05 --max-order-usdc 0 --restart
```

说明：

- copy 模式会直接使用 0.99/0.01 限价跟单，不再因为 `safe/MAX_SLIPPAGE` 跳过正常订单。
- `COPY_RATIO`：跟单比例。
- `MAX_ORDER_USDC`：单笔最大金额，`0` 表示不限制。
- CLOB `/book` 返回 404 时会自动跳过并标记 seen，避免已失效订单簿无限重试。

## AI量化模拟

AI量化建议先跑 `DRY_RUN=1`，记录 24 小时数据再分析。

单边信号记录：

```bash
jy set-bot-mode quant
jy set-dry-run 1
jy set-quant-config --price-source chainlink --chainlink-timeout-sec 20 --chainlink-max-age-sec 240 --chainlink-start-tolerance-sec 180 --record-signals 1 --signal-interval-sec 30
jy quant-data clear
jy app-logs clear
jy service restart
```

JetFadil 风格锁利模拟：

```bash
jy set-bot-mode quant
jy set-dry-run 1
jy set-quant-config --strategy lock --size-mode shares --order-shares 5 --capital-usdc 300 --market-max-usdc 60 --max-trades-per-market 2 --rebuy-cooldown-sec 20 --lock-min-profit 0.50 --min-edge 0.08 --min-seconds-left 10 --max-seconds-left 60 --max-drawdown-usdc 0 --price-source chainlink --chainlink-timeout-sec 20 --chainlink-max-age-sec 240 --chainlink-start-tolerance-sec 180 --record-signals 1 --signal-interval-sec 5
jy quant-data clear
jy app-logs clear
jy service restart
```

`jy quant-data clear` 会同时清空 `data/quant_signals.jsonl` 和 `quant_state.json`，适合重新开始一轮 24 小时模拟。

查看结果：

```bash
jy quant-data summary
jy quant-data tail --lines 10
```

量化数据默认写入：

```text
/opt/polymarket-copy/data/quant_signals.jsonl
```

## VPS 路径

默认安装目录：

```text
/opt/polymarket-copy/
```

配置文件：

```text
/opt/polymarket-copy/.env
```

systemd 服务名：

```text
polymarket-copy
```

文件日志：

```text
/opt/polymarket-copy/logs/polymarket-copy.log
```

## 安全提醒

- 私钥只保存在 VPS 的 `.env` 文件里。
- `.env` 权限会设置为 `600`。
- 不要截图、复制、上传私钥。
- 充值地址不是 `DEPOSIT_WALLET_ADDRESS`。
- 真实下单前必须确认 `jy test` 通过，且日志里没有连续报错。
