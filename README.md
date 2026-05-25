# JY Polymarket Copy CLI

交互式命令行工具，用于部署和管理 Polymarket JetFadil 第一版实时跟单机器人。

第一版采用稳健的混合方案：

- 低频调用 Polymarket Data API `/activity` 发现目标账号的新成交。
- 使用 CLOB API 获取订单簿参数并下单。
- 对 HTTP `429`、`5xx`、网络抖动做自动退避和重试。
- 对 `/book` 做短缓存，减少重复请求。
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
13. 更新程序
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
- `DEPOSIT_WALLET_ADDRESS`: 你自己的 Polymarket Deposit Wallet 地址。
- `CLOB_API_URL`: 默认 `https://clob.polymarket.com`。
- `SIGNATURE_TYPE`: 新 API 用户通常用 `3`。
- `COPY_RATIO`: 跟单比例。
- `POLL_SEC`: 监听间隔。
- `DRY_RUN`: `1` 只打印，`0` 真实下单。
- `TARGET_USERNAME` / `TARGET_WALLET`: 目标账号。

Polymarket CLOB 的 `apiKey / secret / passphrase` 会由 SDK 根据 `PRIVATE_KEY` 自动派生。你一般不需要手动填写 Relayer API Key。

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

菜单 `13. 更新程序` 或 `jy update` 会执行：

- `git pull --ff-only`
- 更新 Python 依赖
- 重新安装本项目
- 重启 `polymarket-copy` 服务

`.env`、`seen_*.json`、`venv/` 都不会被 Git 覆盖。

## 第一版边界

这一版不做历史补仓、不做定时仓位拉平、不做自动撤单、不做盈亏统计。

目标用户成交发现仍使用 Polymarket Data API 的低频 `/activity` 查询。Polymarket 官方 WebSocket 更适合盘口和自己账户通道；任意目标钱包的纯链上 WebSocket 监听会作为后续高级模式。
