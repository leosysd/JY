from __future__ import annotations

import argparse
import contextlib
import getpass
import io
import os
import shlex
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import dotenv_values

from .bot import PolymarketCopyBot, run_configured_bot
from .config import (
    DEFAULT_CLOB_API_URL,
    DEFAULT_MARKET_WS_URL,
    DEFAULT_TARGET_USERNAME,
    DEFAULT_TARGET_WALLET,
    env_lines,
    load_config,
    validate_config,
)


DEFAULT_REMOTE_DIR = "/opt/polymarket-copy"
DEFAULT_SERVICE = "polymarket-copy"
DEFAULT_REPO_URL = "https://github.com/leosysd/JY.git"
DEFAULT_REPO_BRANCH = "main"
ANSI_RESET = "\033[0m"
ANSI_GREEN = "\033[32m"
ANSI_RED = "\033[31m"
ANSI_YELLOW = "\033[33m"
ANSI_CYAN = "\033[36m"


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def prompt_text(label: str, default: str = "", secret: bool = False) -> str:
    if secret and default:
        suffix = " [已设置，回车保留]"
    else:
        suffix = f" [{default}]" if default else ""
    prompt = f"{label}{suffix}: "
    if secret:
        value = getpass.getpass(prompt)
    else:
        value = input(prompt)
    return value.strip() or default


def prompt_yes_no(label: str, default: bool = True) -> bool:
    default_text = "Y/n" if default else "y/N"
    value = input(f"{label} [{default_text}]: ").strip().lower()
    if not value:
        return default
    return value in {"y", "yes", "1", "true", "on"}


def load_existing_env(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    return {
        key: str(value)
        for key, value in dotenv_values(path).items()
        if key and value is not None
    }


def write_env(path: Path, values: Dict[str, str]) -> None:
    path.write_text("\n".join(env_lines(values)) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def init_config(config_path: Path) -> None:
    existing = load_existing_env(config_path)
    target_username = prompt_text(
        "目标用户名 TARGET_USERNAME",
        existing.get("TARGET_USERNAME", DEFAULT_TARGET_USERNAME),
    ).lstrip("@")
    values: Dict[str, str] = {
        "BOT_MODE": prompt_text("运行模式 BOT_MODE，copy=跟单，quant=AI量化", existing.get("BOT_MODE", "copy")).lower(),
        "TARGET_USERNAME": target_username,
        "TARGET_WALLET": prompt_text(
            "目标钱包 TARGET_WALLET",
            existing.get("TARGET_WALLET", DEFAULT_TARGET_WALLET),
        ),
        "PRIVATE_KEY": prompt_text(
            "你的交易钱包私钥 PRIVATE_KEY",
            existing.get("PRIVATE_KEY", ""),
            secret=True,
        ),
        "CHAIN_ID": prompt_text("链 ID CHAIN_ID", existing.get("CHAIN_ID", "137")),
        "CLOB_API_URL": prompt_text(
            "CLOB API 地址 CLOB_API_URL",
            existing.get("CLOB_API_URL", DEFAULT_CLOB_API_URL),
        ),
        "ENABLE_MARKET_WS": (
            "1"
            if prompt_yes_no(
                "启用 Market WebSocket 盘口缓存吗",
                existing.get("ENABLE_MARKET_WS", "1") != "0",
            )
            else "0"
        ),
        "MARKET_WS_URL": prompt_text(
            "Market WebSocket 地址 MARKET_WS_URL",
            existing.get("MARKET_WS_URL", DEFAULT_MARKET_WS_URL),
        ),
        "SIGNATURE_TYPE": prompt_text(
            "签名类型 SIGNATURE_TYPE",
            existing.get("SIGNATURE_TYPE", "3"),
        ),
        "DEPOSIT_WALLET_ADDRESS": prompt_text(
            "你的 Funder/API 地址 DEPOSIT_WALLET_ADDRESS",
            existing.get("DEPOSIT_WALLET_ADDRESS", ""),
        ),
        "POLL_SEC": prompt_text("轮询间隔秒数 POLL_SEC", existing.get("POLL_SEC", "1")),
        "COPY_RATIO": prompt_text("跟单比例 COPY_RATIO", existing.get("COPY_RATIO", "1.0")),
        "PRICE_MODE": prompt_text("价格保护 PRICE_MODE，safe=保护价格，aggressive=强制成交", existing.get("PRICE_MODE", "safe")),
        "MAX_SLIPPAGE": prompt_text("最大滑点 MAX_SLIPPAGE，0.02=2分钱", existing.get("MAX_SLIPPAGE", "0.02")),
        "MAX_ORDER_USDC": prompt_text("单笔最大金额 MAX_ORDER_USDC，0=不限制", existing.get("MAX_ORDER_USDC", "0")),
        "DRY_RUN": "1" if prompt_yes_no("先使用 DRY_RUN 只打印不下单吗", True) else "0",
        "STATE_FILE": existing.get("STATE_FILE", f"seen_{target_username}.json"),
        "ACTIVITY_LIMIT": existing.get("ACTIVITY_LIMIT", "100"),
        "BOOTSTRAP_LIMIT": existing.get("BOOTSTRAP_LIMIT", "500"),
        "BOOK_CACHE_SEC": existing.get("BOOK_CACHE_SEC", "300"),
        "HTTP_TIMEOUT_SEC": existing.get("HTTP_TIMEOUT_SEC", "10"),
        "HTTP_MAX_RETRIES": existing.get("HTTP_MAX_RETRIES", "5"),
        "BACKOFF_BASE_SEC": existing.get("BACKOFF_BASE_SEC", "0.5"),
        "BACKOFF_MAX_SEC": existing.get("BACKOFF_MAX_SEC", "20"),
        "MARK_FAILED_SEEN": existing.get("MARK_FAILED_SEEN", "1"),
        "MARKET_WS_BOOTSTRAP_ASSETS": existing.get("MARKET_WS_BOOTSTRAP_ASSETS", "100"),
        "MARKET_WS_MAX_ASSETS": existing.get("MARKET_WS_MAX_ASSETS", "250"),
        "MARKET_WS_BOOK_MAX_AGE_SEC": existing.get("MARKET_WS_BOOK_MAX_AGE_SEC", "900"),
        "MARKET_WS_HEARTBEAT_SEC": existing.get("MARKET_WS_HEARTBEAT_SEC", "10"),
        "MARKET_WS_RECONNECT_SEC": existing.get("MARKET_WS_RECONNECT_SEC", "3"),
        "MARKET_WS_CUSTOM_FEATURE_ENABLED": existing.get("MARKET_WS_CUSTOM_FEATURE_ENABLED", "1"),
        "QUANT_SYMBOL": existing.get("QUANT_SYMBOL", "BTC-USDT"),
        "QUANT_MARKET_SLUG_PREFIX": existing.get("QUANT_MARKET_SLUG_PREFIX", "btc-updown-5m"),
        "QUANT_PRICE_SOURCE": existing.get("QUANT_PRICE_SOURCE", "okx"),
        "QUANT_ORDER_USDC": existing.get("QUANT_ORDER_USDC", "5"),
        "QUANT_MIN_EDGE": existing.get("QUANT_MIN_EDGE", "0.04"),
        "QUANT_MIN_SECONDS_LEFT": existing.get("QUANT_MIN_SECONDS_LEFT", "45"),
        "QUANT_COOLDOWN_SEC": existing.get("QUANT_COOLDOWN_SEC", "60"),
        "QUANT_LOG_INTERVAL_SEC": existing.get("QUANT_LOG_INTERVAL_SEC", "10"),
        "QUANT_STATE_FILE": existing.get("QUANT_STATE_FILE", "quant_state.json"),
    }
    write_env(config_path, values)
    print(f"[OK] 已写入 {config_path}")


def print_config_summary(config_path: Path, require_private_key: Optional[bool] = None) -> bool:
    config = load_config(config_path)
    if require_private_key is None:
        require_private_key = not config.dry_run
    errors, warnings = validate_config(config, require_private_key=require_private_key)
    print(f"配置文件: {config_path}")
    print(f"运行模式: {config.bot_mode}")
    print(f"目标: @{config.target_username} / {config.target_wallet}")
    print(f"模式: {config.mode_label}")
    print(f"跟单比例: {config.copy_ratio}")
    print(f"价格保护: {config.price_mode}, 最大滑点: {config.max_slippage}, 单笔上限: {config.max_order_usdc} USDC")
    print(
        "AI量化: "
        f"{config.quant_symbol}, order_usdc={config.quant_order_usdc}, "
        f"min_edge={config.quant_min_edge}, min_seconds_left={config.quant_min_seconds_left}"
    )
    print(f"轮询间隔: {config.poll_sec}s")
    print(f"Market WS: {'开启' if config.enable_market_ws else '关闭'}")
    print(f"私钥: {config.masked_private_key}")
    print(f"Funder/API 地址: {config.deposit_wallet_address or '<empty>'}")
    for warning in warnings:
        print(f"[WARN] {warning}")
    for error in errors:
        print(f"[ERROR] {error}")
    if not errors:
        print("[OK] 配置校验通过")
    return not errors


def run_bot(config_path: Path) -> None:
    config = load_config(config_path)
    run_configured_bot(config)


def remote_target(host: str, user: str) -> str:
    return f"{user}@{host}" if user else host


def sh_quote(value: str) -> str:
    return shlex.quote(value)


def run_command(argv: List[str], input_text: Optional[str] = None) -> None:
    printable = " ".join(shlex.quote(part) for part in argv)
    print(f"[RUN] {printable}")
    subprocess.run(argv, input=input_text, text=True, check=True)


def ssh(target: str, command: str) -> None:
    run_command(["ssh", target, command])


def scp(src: Path, dest: str, recursive: bool = False) -> None:
    argv = ["scp"]
    if recursive:
        argv.append("-r")
    argv.extend([str(src), dest])
    run_command(argv)


def sudo_prefix(user: str) -> str:
    return "" if user == "root" else "sudo "


def service_content(remote_dir: str) -> str:
    return f"""[Unit]
Description=Polymarket Realtime Copy Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={remote_dir}
EnvironmentFile={remote_dir}/.env
Environment=PYTHONUNBUFFERED=1
ExecStart={remote_dir}/venv/bin/polymarket-copy-bot --config {remote_dir}/.env
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
"""


def install_from_git(
    host: str,
    user: str,
    remote_dir: str,
    config_path: Path,
    service_name: str,
    repo_url: str,
    branch: str,
    install_system_deps: bool,
) -> None:
    if not config_path.exists():
        raise SystemExit(f"配置文件不存在: {config_path}，请先运行 init-config")
    if not print_config_summary(config_path):
        raise SystemExit("配置校验失败，请先修正 .env")

    target = remote_target(host, user)
    sudo = sudo_prefix(user)
    remote_q = sh_quote(remote_dir)
    repo_q = sh_quote(repo_url)
    branch_q = sh_quote(branch)

    if install_system_deps:
        ssh(
            target,
            f"{sudo}apt-get update && "
            f"{sudo}apt-get install -y python3 python3-venv python3-pip git",
        )

    ssh(
        target,
        f"{sudo}mkdir -p {remote_q} && "
        f"{sudo}chown -R $USER:$USER {remote_q} && "
        f"if [ -d {remote_q}/.git ]; then "
        f"cd {remote_q} && git fetch origin {branch_q} && git checkout {branch_q} && git pull --ff-only origin {branch_q}; "
        f"elif [ -z \"$(ls -A {remote_q} 2>/dev/null)\" ]; then "
        f"git clone --branch {branch_q} {repo_q} {remote_q}; "
        f"else "
        f"echo 'Remote directory exists and is not an empty Git repo: {remote_dir}' >&2; exit 2; "
        f"fi",
    )

    scp(config_path, f"{target}:{remote_dir}/.env")
    ssh(
        target,
        f"cd {remote_q} && "
        "python3 -m venv venv && "
        "./venv/bin/pip install --upgrade pip && "
        "./venv/bin/pip install -r requirements.txt && "
        "./venv/bin/pip install -e . && "
        "chmod 600 .env",
    )

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".service", delete=False) as fh:
        fh.write(service_content(remote_dir))
        local_service = Path(fh.name)
    try:
        remote_tmp = f"/tmp/{service_name}.service"
        scp(local_service, f"{target}:{remote_tmp}")
        ssh(
            target,
            f"{sudo}mv {sh_quote(remote_tmp)} /etc/systemd/system/{sh_quote(service_name)}.service && "
            f"{sudo}systemctl daemon-reload && "
            f"{sudo}systemctl enable {sh_quote(service_name)} && "
            f"{sudo}systemctl restart {sh_quote(service_name)}",
        )
    finally:
        try:
            local_service.unlink()
        except OSError:
            pass

    print("[OK] Git 安装完成")
    print(f"以后更新: jy-cli remote update --host {host} --user {user}")


def deploy_to_vps(
    host: str,
    user: str,
    remote_dir: str,
    config_path: Path,
    service_name: str,
    install_system_deps: bool,
) -> None:
    if not config_path.exists():
        raise SystemExit(f"配置文件不存在: {config_path}，请先运行 init-config")
    if not print_config_summary(config_path):
        raise SystemExit("配置校验失败，请先修正 .env")

    root = project_root()
    target = remote_target(host, user)
    sudo = sudo_prefix(user)
    remote_q = sh_quote(remote_dir)

    ssh(target, f"{sudo}mkdir -p {remote_q} && {sudo}chown $USER:$USER {remote_q}")
    if install_system_deps:
        ssh(
            target,
            f"{sudo}apt-get update && "
            f"{sudo}apt-get install -y python3 python3-venv python3-pip git",
        )

    upload_items = [
        root / "pyproject.toml",
        root / "requirements.txt",
        root / "README.md",
        root / ".env.example",
    ]
    for item in upload_items:
        scp(item, f"{target}:{remote_dir}/")
    scp(root / "polymarket_copy", f"{target}:{remote_dir}/", recursive=True)
    scp(config_path, f"{target}:{remote_dir}/.env")

    ssh(
        target,
        f"cd {remote_q} && "
        "python3 -m venv venv && "
        "./venv/bin/pip install --upgrade pip && "
        "./venv/bin/pip install -r requirements.txt && "
        "./venv/bin/pip install -e . && "
        "chmod 600 .env",
    )

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".service", delete=False) as fh:
        fh.write(service_content(remote_dir))
        local_service = Path(fh.name)
    try:
        remote_tmp = f"/tmp/{service_name}.service"
        scp(local_service, f"{target}:{remote_tmp}")
        ssh(
            target,
            f"{sudo}mv {sh_quote(remote_tmp)} /etc/systemd/system/{sh_quote(service_name)}.service && "
            f"{sudo}systemctl daemon-reload && "
            f"{sudo}systemctl enable {sh_quote(service_name)} && "
            f"{sudo}systemctl restart {sh_quote(service_name)}",
        )
    finally:
        try:
            local_service.unlink()
        except OSError:
            pass

    print("[OK] 部署完成")
    print(f"查看状态: jy-cli remote status --host {host} --user {user}")
    print(f"查看日志: jy-cli remote logs --host {host} --user {user}")


def remote_action(
    action: str,
    host: str,
    user: str,
    remote_dir: str,
    service_name: str,
    dry_run_value: Optional[str],
) -> None:
    target = remote_target(host, user)
    sudo = sudo_prefix(user)
    svc = sh_quote(service_name)
    remote_q = sh_quote(remote_dir)

    if action == "status":
        ssh(target, f"{sudo}systemctl status {svc} --no-pager")
    elif action == "logs":
        ssh(target, f"{sudo}journalctl -u {svc} -f -n 100")
    elif action in {"start", "stop", "restart"}:
        ssh(target, f"{sudo}systemctl {action} {svc}")
    elif action == "update":
        cmd = (
            f"cd {remote_q} && "
            "if [ ! -d .git ]; then "
            "echo 'Remote directory is not a Git install. Reinstall with jy-cli install first.' >&2; "
            "exit 2; "
            "fi && "
            "git pull --ff-only && "
            "python3 -m venv venv && "
            "./venv/bin/pip install --upgrade pip && "
            "./venv/bin/pip install -r requirements.txt && "
            "./venv/bin/pip install -e . && "
            f"{sudo}systemctl restart {svc}"
        )
        ssh(target, cmd)
        print("[OK] 远程程序已更新并重启")
    elif action == "dry-run":
        if dry_run_value not in {"0", "1"}:
            raise SystemExit("--value 必须是 0 或 1")
        cmd = (
            f"cd {remote_q} && "
            f"if grep -q '^DRY_RUN=' .env; then "
            f"sed -i 's/^DRY_RUN=.*/DRY_RUN={dry_run_value}/' .env; "
            f"else printf '\\nDRY_RUN={dry_run_value}\\n' >> .env; fi && "
            f"chmod 600 .env && "
            f"{sudo}systemctl restart {svc}"
        )
        ssh(target, cmd)
        mode = "只打印" if dry_run_value == "1" else "真实下单"
        print(f"[OK] 远程 DRY_RUN={dry_run_value}，当前模式: {mode}")
    else:
        raise SystemExit(f"未知远程动作: {action}")


def command_with_sudo(argv: List[str]) -> List[str]:
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        return ["sudo", *argv]
    return argv


def local_service_path(service_name: str = DEFAULT_SERVICE) -> str:
    return f"/etc/systemd/system/{service_name}.service"


def local_install_dir() -> Path:
    return Path.cwd().resolve()


def install_local_service(
    install_dir: Path,
    service_name: str = DEFAULT_SERVICE,
    start_now: bool = False,
) -> None:
    service = service_content(str(install_dir))
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".service", delete=False) as fh:
        fh.write(service)
        tmp_path = Path(fh.name)
    try:
        run_command(command_with_sudo(["install", "-m", "644", str(tmp_path), local_service_path(service_name)]))
        run_command(command_with_sudo(["systemctl", "daemon-reload"]))
        run_command(command_with_sudo(["systemctl", "disable", service_name]))
        if start_now:
            run_command(command_with_sudo(["systemctl", "restart", service_name]))
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass
    print(f"[OK] systemd 服务已安装: {service_name}，开机自启已关闭")


def local_service_action(action: str, service_name: str = DEFAULT_SERVICE) -> None:
    if action == "status":
        run_command(command_with_sudo(["systemctl", "status", service_name, "--no-pager"]))
    elif action == "logs":
        run_command(command_with_sudo(["journalctl", "-u", service_name, "-f", "-n", "100"]))
    elif action in {"start", "stop", "restart"}:
        run_command(command_with_sudo(["systemctl", action, service_name]))
    elif action == "disable-autostart":
        run_command(command_with_sudo(["systemctl", "disable", service_name]))
        print(f"[OK] 已关闭开机自启: {service_name}")
    else:
        raise SystemExit(f"未知本地服务动作: {action}")


def color_text(text: str, color: str) -> str:
    return f"{color}{text}{ANSI_RESET}"


def systemctl_value(action: str, service_name: str = DEFAULT_SERVICE) -> str:
    try:
        result = subprocess.run(
            ["systemctl", action, service_name],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return "unknown"
    combined = f"{result.stdout}\n{result.stderr}".strip()
    if "System has not been booted with systemd" in combined:
        return "systemd-unavailable"
    value = (result.stdout or result.stderr).strip()
    if not value:
        return "unknown"
    return value.splitlines()[0].strip()


def service_status_text(active: str) -> str:
    if active == "active":
        return color_text("运行中", ANSI_GREEN)
    if active == "inactive":
        return color_text("未启动", ANSI_RED)
    if active == "failed":
        return color_text("启动失败", ANSI_RED)
    if active == "activating":
        return color_text("启动中", ANSI_YELLOW)
    if active == "deactivating":
        return color_text("停止中", ANSI_YELLOW)
    if active == "systemd-unavailable":
        return color_text("systemd不可用", ANSI_YELLOW)
    return color_text(active, ANSI_YELLOW)


def autostart_status_text(enabled: str) -> str:
    if enabled == "enabled":
        return color_text("开机自启", ANSI_YELLOW)
    if enabled == "disabled":
        return color_text("不开机自启", ANSI_GREEN)
    if enabled == "not-found":
        return color_text("服务未安装", ANSI_RED)
    if enabled == "systemd-unavailable":
        return color_text("systemd不可用", ANSI_YELLOW)
    return color_text(enabled, ANSI_YELLOW)


def mode_status_text(mode_label: str) -> str:
    if mode_label == "DRY_RUN":
        return color_text("只打印", ANSI_YELLOW)
    return color_text("真实下单", ANSI_RED)


def config_status_text(errors: List[str], warnings: List[str]) -> str:
    if errors:
        return color_text(f"需检查 {len(errors)} 项", ANSI_RED)
    if warnings:
        return color_text(f"OK，警告 {len(warnings)} 项", ANSI_YELLOW)
    return color_text("OK", ANSI_GREEN)


def service_state_label(service_name: str = DEFAULT_SERVICE) -> str:
    active = systemctl_value("is-active", service_name)
    enabled = systemctl_value("is-enabled", service_name)
    return f"{service_status_text(active)} / {autostart_status_text(enabled)}"


def menu_status_line(config_path: Path, service_name: str = DEFAULT_SERVICE) -> str:
    service_state = service_state_label(service_name)
    if not config_path.exists():
        return f"服务: {service_state} | 配置: 未创建 .env"
    try:
        config = load_config(config_path)
        errors, warnings = validate_config(config, require_private_key=not config.dry_run)
    except Exception as exc:
        return f"服务: {service_state} | 配置: 读取失败 ({exc})"
    return (
        f"服务: {service_state} | 配置: {config_status_text(errors, warnings)} | "
        f"策略: {config.bot_mode} | "
        f"模式: {mode_status_text(config.mode_label)} | "
        f"价格: {config.price_mode}/{config.max_slippage} | "
        f"目标: {color_text('@' + config.target_username, ANSI_CYAN)}"
    )


def set_env_value(config_path: Path, key: str, value: str) -> None:
    lines: List[str] = []
    found = False
    if config_path.exists():
        lines = config_path.read_text(encoding="utf-8").splitlines()
    for idx, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[idx] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(config_path, 0o600)
    except OSError:
        pass


def update_copy_ratio(config_path: Path) -> None:
    existing = load_existing_env(config_path)
    value = prompt_text("新的跟单比例 COPY_RATIO", existing.get("COPY_RATIO", "1.0"))
    set_env_value(config_path, "COPY_RATIO", value)
    print(f"[OK] COPY_RATIO={value}")


def update_price_protection(config_path: Path) -> None:
    existing = load_existing_env(config_path)
    mode = prompt_text(
        "价格模式 PRICE_MODE，safe=保护价格，aggressive=强制成交",
        existing.get("PRICE_MODE", "safe"),
    ).lower()
    if mode not in {"safe", "aggressive"}:
        raise SystemExit("PRICE_MODE 必须是 safe 或 aggressive")
    slippage = prompt_text(
        "最大滑点 MAX_SLIPPAGE，0.02=2分钱",
        existing.get("MAX_SLIPPAGE", "0.02"),
    )
    max_usdc = prompt_text(
        "单笔最大金额 MAX_ORDER_USDC，0=不限制",
        existing.get("MAX_ORDER_USDC", "0"),
    )
    set_env_value(config_path, "PRICE_MODE", mode)
    set_env_value(config_path, "MAX_SLIPPAGE", slippage)
    set_env_value(config_path, "MAX_ORDER_USDC", max_usdc)
    print(
        f"[OK] PRICE_MODE={mode}, MAX_SLIPPAGE={slippage}, "
        f"MAX_ORDER_USDC={max_usdc}"
    )


def update_bot_mode(config_path: Path, value: Optional[str] = None) -> None:
    existing = load_existing_env(config_path)
    mode = (value or prompt_text("运行模式 BOT_MODE，copy=跟单，quant=AI量化", existing.get("BOT_MODE", "copy"))).lower()
    if mode not in {"copy", "quant"}:
        raise SystemExit("BOT_MODE 必须是 copy 或 quant")
    set_env_value(config_path, "BOT_MODE", mode)
    print(f"[OK] BOT_MODE={mode}")


def update_quant_config(config_path: Path) -> None:
    existing = load_existing_env(config_path)
    values = {
        "QUANT_SYMBOL": prompt_text("量化交易对 QUANT_SYMBOL", existing.get("QUANT_SYMBOL", "BTC-USDT")),
        "QUANT_PRICE_SOURCE": prompt_text("行情源 QUANT_PRICE_SOURCE，第一版填 okx", existing.get("QUANT_PRICE_SOURCE", "okx")),
        "QUANT_ORDER_USDC": prompt_text("每次量化下单金额 QUANT_ORDER_USDC", existing.get("QUANT_ORDER_USDC", "5")),
        "QUANT_MIN_EDGE": prompt_text("最小优势 QUANT_MIN_EDGE，0.04=4分钱", existing.get("QUANT_MIN_EDGE", "0.04")),
        "QUANT_MIN_SECONDS_LEFT": prompt_text("最少剩余秒数 QUANT_MIN_SECONDS_LEFT", existing.get("QUANT_MIN_SECONDS_LEFT", "45")),
        "QUANT_COOLDOWN_SEC": prompt_text("量化下单冷却秒数 QUANT_COOLDOWN_SEC", existing.get("QUANT_COOLDOWN_SEC", "60")),
        "QUANT_LOG_INTERVAL_SEC": prompt_text("量化日志间隔秒数 QUANT_LOG_INTERVAL_SEC", existing.get("QUANT_LOG_INTERVAL_SEC", "10")),
        "QUANT_STATE_FILE": prompt_text("量化状态文件 QUANT_STATE_FILE", existing.get("QUANT_STATE_FILE", "quant_state.json")),
    }
    for key, value in values.items():
        set_env_value(config_path, key, value)
    print("[OK] AI量化参数已更新")


def quant_once(config_path: Path) -> None:
    from .quant import PolymarketQuantBot

    fd, tmp_name = tempfile.mkstemp(prefix="jy_quant_once_", suffix=".json")
    os.close(fd)
    tmp_state = Path(tmp_name)
    try:
        config = replace(
            load_config(config_path),
            bot_mode="quant",
            dry_run=True,
            quant_state_file=tmp_state,
        )
        print("[INFO] AI量化单次试算强制使用 DRY_RUN，不会真实下单。")
        decision = PolymarketQuantBot(config).run_once(client=None)
        if decision is None:
            print("[INFO] 本轮没有量化买入信号。")
    finally:
        try:
            tmp_state.unlink()
        except OSError:
            pass


def update_target(config_path: Path) -> None:
    existing = load_existing_env(config_path)
    username = prompt_text("目标用户名 TARGET_USERNAME", existing.get("TARGET_USERNAME", DEFAULT_TARGET_USERNAME)).lstrip("@")
    wallet = prompt_text("目标钱包 TARGET_WALLET", existing.get("TARGET_WALLET", DEFAULT_TARGET_WALLET))
    set_env_value(config_path, "TARGET_USERNAME", username)
    set_env_value(config_path, "TARGET_WALLET", wallet)
    if not existing.get("STATE_FILE"):
        set_env_value(config_path, "STATE_FILE", f"seen_{username}.json")
    print("[OK] 目标账号已更新")


def update_dry_run(config_path: Path, value: Optional[str] = None) -> None:
    if value not in {"0", "1"}:
        value = prompt_text("DRY_RUN 值，1=只打印，0=真实下单", "1")
    if value not in {"0", "1"}:
        raise SystemExit("DRY_RUN 必须是 0 或 1")
    set_env_value(config_path, "DRY_RUN", value)
    mode = "只打印" if value == "1" else "真实下单"
    print(f"[OK] DRY_RUN={value}，当前模式: {mode}")


def update_program(service_name: str = DEFAULT_SERVICE) -> None:
    install_dir = local_install_dir()
    if not (install_dir / ".git").exists():
        raise SystemExit("当前目录不是 Git 安装目录，无法自动更新。请在 /opt/polymarket-copy 运行 jy。")
    run_command(["git", "pull", "--ff-only"])
    run_command([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])
    run_command([sys.executable, "-m", "pip", "install", "-e", "."])
    try:
        local_service_action("restart", service_name)
    except subprocess.CalledProcessError as exc:
        print(f"[WARN] 程序已更新，但服务重启失败，退出码 {exc.returncode}")
    print("[OK] 程序更新完成")
    print("[INFO] 当前菜单进程仍是更新前版本。请退出后重新运行 jy 使用新版菜单。")


def test_api_config(config_path: Path) -> bool:
    if not config_path.exists():
        print(f"[ERROR] 配置文件不存在: {config_path}")
        return False
    config = load_config(config_path)
    errors, warnings = validate_config(config, require_private_key=not config.dry_run)
    for warning in warnings:
        print(f"[WARN] {warning}")
    if errors:
        for error in errors:
            print(f"[ERROR] {error}")
        return False

    bot = PolymarketCopyBot(config)
    try:
        wallet = bot.resolve_target_wallet()
        print(f"[OK] 目标钱包: {wallet}")
        print(
            "[OK] Market WebSocket 配置: "
            f"{'开启' if config.enable_market_ws else '关闭'} "
            f"{config.market_ws_url if config.enable_market_ws else ''}".strip()
        )
        print(
            "[OK] 价格保护: "
            f"PRICE_MODE={config.price_mode}, "
            f"MAX_SLIPPAGE={config.max_slippage}, "
            f"MAX_ORDER_USDC={config.max_order_usdc}"
        )
        activities = bot.fetch_activity(wallet, limit=3)
        print(f"[OK] Data API 可访问，最近 TRADE 数量: {len(activities)}")
        if activities:
            token_id = str(activities[0].get("asset", ""))
            if token_id:
                book = bot.book_cache.get_book(token_id)
                print(
                    "[OK] CLOB /book 可访问，"
                    f"tick_size={book.get('tick_size')} min_order_size={book.get('min_order_size')}"
                )
        if config.private_key:
            sdk_output = io.StringIO()
            try:
                with contextlib.redirect_stdout(sdk_output), contextlib.redirect_stderr(sdk_output):
                    bot.build_client()
                captured = sdk_output.getvalue().strip()
                if captured:
                    if "Could not create api key" in captured or "/auth/api-key" in captured:
                        print("[INFO] SDK 创建新 API key 的尝试有提示，但已成功派生/加载可用凭证。")
                    else:
                        print(f"[INFO] SDK 输出: {captured}")
                print("[OK] CLOB API 凭证可自动派生，签名客户端初始化成功")
            except Exception as exc:
                captured = sdk_output.getvalue().strip()
                if captured:
                    print(f"[INFO] SDK 输出: {captured}")
                print(f"[ERROR] CLOB 签名/认证测试失败: {exc}")
                return False
        else:
            print("[WARN] PRIVATE_KEY 未设置，只能监控或 DRY_RUN，不能自动下单")
    except Exception as exc:
        print(f"[ERROR] API 测试失败: {exc}")
        return False
    return True


def local_interactive_menu(config_path: Path = Path(".env")) -> None:
    while True:
        print("")
        print("JY Polymarket Copy CLI")
        print(menu_status_line(config_path))
        print("1. 初始化/修改交易配置")
        print("2. 查看当前配置")
        print("3. 测试 API/私钥/签名")
        print("4. 安装/刷新 systemd 服务")
        print("5. 启动服务")
        print("6. 停止服务")
        print("7. 重启服务")
        print("8. 查看服务状态")
        print("9. 查看实时日志")
        print("10. 切换 DRY_RUN")
        print("11. 修改跟单比例 COPY_RATIO")
        print("12. 修改目标用户/钱包")
        print("13. 修改价格保护")
        print("14. 关闭开机自启")
        print("15. 切换策略模式 BOT_MODE")
        print("16. 修改 AI量化参数")
        print("17. AI量化单次试算")
        print("18. 更新程序")
        print("0. 退出")
        choice = input("请选择: ").strip()
        try:
            if choice == "1":
                init_config(config_path)
            elif choice == "2":
                print_config_summary(config_path)
            elif choice == "3":
                test_api_config(config_path)
            elif choice == "4":
                ok = print_config_summary(config_path)
                install_local_service(local_install_dir(), start_now=ok)
            elif choice == "5":
                local_service_action("start")
            elif choice == "6":
                local_service_action("stop")
            elif choice == "7":
                local_service_action("restart")
            elif choice == "8":
                local_service_action("status")
            elif choice == "9":
                local_service_action("logs")
            elif choice == "10":
                update_dry_run(config_path)
                if prompt_yes_no("是否立即重启服务让配置生效", True):
                    local_service_action("restart")
            elif choice == "11":
                update_copy_ratio(config_path)
                if prompt_yes_no("是否立即重启服务让配置生效", True):
                    local_service_action("restart")
            elif choice == "12":
                update_target(config_path)
                if prompt_yes_no("是否立即重启服务让配置生效", True):
                    local_service_action("restart")
            elif choice == "13":
                update_price_protection(config_path)
                if prompt_yes_no("是否立即重启服务让配置生效", True):
                    local_service_action("restart")
            elif choice == "14":
                local_service_action("disable-autostart")
            elif choice == "15":
                update_bot_mode(config_path)
                if prompt_yes_no("是否立即重启服务让配置生效", True):
                    local_service_action("restart")
            elif choice == "16":
                update_quant_config(config_path)
                if prompt_yes_no("是否立即重启服务让配置生效", True):
                    local_service_action("restart")
            elif choice == "17":
                quant_once(config_path)
            elif choice == "18":
                update_program()
                print("[INFO] 请重新运行 jy。")
                return
            elif choice == "0":
                return
            else:
                print("无效选项")
        except KeyboardInterrupt:
            print("")
            return
        except subprocess.CalledProcessError as exc:
            print(f"[ERROR] 命令执行失败，退出码 {exc.returncode}")
        except Exception as exc:
            print(f"[ERROR] {exc}")


def interactive_menu() -> None:
    local_interactive_menu()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Interactive CLI for Polymarket copy bot.")
    sub = parser.add_subparsers(dest="command")

    p_menu = sub.add_parser("menu", help="打开 VPS 本地交互菜单")
    p_menu.add_argument("--config", default=".env")

    p_init = sub.add_parser("init-config", help="交互式生成 .env")
    p_init.add_argument("--config", default=".env")

    p_validate = sub.add_parser("validate", help="校验配置")
    p_validate.add_argument("--config", default=".env")

    p_test = sub.add_parser("test", help="测试 API、私钥和签名客户端")
    p_test.add_argument("--config", default=".env")

    p_run = sub.add_parser("run", help="本地运行机器人")
    p_run.add_argument("--config", default=".env")

    p_service = sub.add_parser("service", help="管理 VPS 本机 systemd 服务")
    p_service.add_argument(
        "action",
        choices=["install", "start", "stop", "restart", "status", "logs", "disable-autostart"],
    )
    p_service.add_argument("--service-name", default=DEFAULT_SERVICE)
    p_service.add_argument("--start-now", action="store_true", help="install 后立即启动/重启服务")

    p_update = sub.add_parser("update", help="在 VPS 本机 git pull 并重启服务")
    p_update.add_argument("--service-name", default=DEFAULT_SERVICE)

    p_dry = sub.add_parser("set-dry-run", help="设置 DRY_RUN，1=只打印，0=真实下单")
    p_dry.add_argument("value", choices=["0", "1"])
    p_dry.add_argument("--config", default=".env")
    p_dry.add_argument("--restart", action="store_true", help="设置后立即重启服务")

    p_price = sub.add_parser("set-price-protection", help="设置价格保护和滑点")
    p_price.add_argument("--config", default=".env")
    p_price.add_argument("--mode", choices=["safe", "aggressive"])
    p_price.add_argument("--max-slippage")
    p_price.add_argument("--max-order-usdc")
    p_price.add_argument("--restart", action="store_true", help="设置后立即重启服务")

    p_mode = sub.add_parser("set-bot-mode", help="设置 BOT_MODE，copy=跟单，quant=AI量化")
    p_mode.add_argument("value", choices=["copy", "quant"])
    p_mode.add_argument("--config", default=".env")
    p_mode.add_argument("--restart", action="store_true", help="设置后立即重启服务")

    p_quant = sub.add_parser("set-quant-config", help="设置 AI量化参数")
    p_quant.add_argument("--config", default=".env")
    p_quant.add_argument("--symbol")
    p_quant.add_argument("--price-source")
    p_quant.add_argument("--order-usdc")
    p_quant.add_argument("--min-edge")
    p_quant.add_argument("--min-seconds-left")
    p_quant.add_argument("--cooldown-sec")
    p_quant.add_argument("--log-interval-sec")
    p_quant.add_argument("--restart", action="store_true", help="设置后立即重启服务")

    p_quant_once = sub.add_parser("quant-once", help="AI量化单次试算，不真实下单")
    p_quant_once.add_argument("--config", default=".env")

    p_install = sub.add_parser("install", help="从 GitHub 安装到 VPS，便于以后远程更新")
    p_install.add_argument("--host", required=True)
    p_install.add_argument("--user", default="root")
    p_install.add_argument("--remote-dir", default=DEFAULT_REMOTE_DIR)
    p_install.add_argument("--config", default=".env")
    p_install.add_argument("--service-name", default=DEFAULT_SERVICE)
    p_install.add_argument("--repo-url", default=DEFAULT_REPO_URL)
    p_install.add_argument("--branch", default=DEFAULT_REPO_BRANCH)
    p_install.add_argument(
        "--skip-system-deps",
        action="store_true",
        help="跳过 apt 安装系统依赖",
    )

    p_deploy = sub.add_parser("deploy", help="部署到 VPS")
    p_deploy.add_argument("--host", required=True)
    p_deploy.add_argument("--user", default="root")
    p_deploy.add_argument("--remote-dir", default=DEFAULT_REMOTE_DIR)
    p_deploy.add_argument("--config", default=".env")
    p_deploy.add_argument("--service-name", default=DEFAULT_SERVICE)
    p_deploy.add_argument(
        "--skip-system-deps",
        action="store_true",
        help="跳过 apt 安装系统依赖",
    )

    p_remote = sub.add_parser("remote", help="管理 VPS 上的 systemd 服务")
    p_remote.add_argument(
        "action",
        choices=["status", "logs", "start", "stop", "restart", "update", "dry-run"],
    )
    p_remote.add_argument("--host", required=True)
    p_remote.add_argument("--user", default="root")
    p_remote.add_argument("--remote-dir", default=DEFAULT_REMOTE_DIR)
    p_remote.add_argument("--service-name", default=DEFAULT_SERVICE)
    p_remote.add_argument("--value", choices=["0", "1"], help="dry-run 动作使用")

    return parser


def main(argv: Optional[List[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        interactive_menu()
        return

    if args.command == "menu":
        local_interactive_menu(Path(args.config))
    elif args.command == "init-config":
        init_config(Path(args.config))
    elif args.command == "validate":
        ok = print_config_summary(Path(args.config))
        raise SystemExit(0 if ok else 1)
    elif args.command == "test":
        ok = test_api_config(Path(args.config))
        raise SystemExit(0 if ok else 1)
    elif args.command == "run":
        run_bot(Path(args.config))
    elif args.command == "service":
        if args.action == "install":
            install_local_service(local_install_dir(), args.service_name, start_now=args.start_now)
        else:
            local_service_action(args.action, args.service_name)
    elif args.command == "update":
        update_program(args.service_name)
    elif args.command == "set-dry-run":
        update_dry_run(Path(args.config), args.value)
        if args.restart:
            local_service_action("restart")
    elif args.command == "set-price-protection":
        config_path = Path(args.config)
        if args.mode is None and args.max_slippage is None and args.max_order_usdc is None:
            update_price_protection(config_path)
        else:
            existing = load_existing_env(config_path)
            set_env_value(config_path, "PRICE_MODE", args.mode or existing.get("PRICE_MODE", "safe"))
            set_env_value(config_path, "MAX_SLIPPAGE", args.max_slippage or existing.get("MAX_SLIPPAGE", "0.02"))
            set_env_value(config_path, "MAX_ORDER_USDC", args.max_order_usdc or existing.get("MAX_ORDER_USDC", "0"))
            print("[OK] 价格保护配置已更新")
        if args.restart:
            local_service_action("restart")
    elif args.command == "set-bot-mode":
        update_bot_mode(Path(args.config), args.value)
        if args.restart:
            local_service_action("restart")
    elif args.command == "set-quant-config":
        config_path = Path(args.config)
        if not any(
            [
                args.symbol,
                args.price_source,
                args.order_usdc,
                args.min_edge,
                args.min_seconds_left,
                args.cooldown_sec,
                args.log_interval_sec,
            ]
        ):
            update_quant_config(config_path)
        else:
            existing = load_existing_env(config_path)
            set_env_value(config_path, "QUANT_SYMBOL", args.symbol or existing.get("QUANT_SYMBOL", "BTC-USDT"))
            set_env_value(config_path, "QUANT_PRICE_SOURCE", args.price_source or existing.get("QUANT_PRICE_SOURCE", "okx"))
            set_env_value(config_path, "QUANT_ORDER_USDC", args.order_usdc or existing.get("QUANT_ORDER_USDC", "5"))
            set_env_value(config_path, "QUANT_MIN_EDGE", args.min_edge or existing.get("QUANT_MIN_EDGE", "0.04"))
            set_env_value(
                config_path,
                "QUANT_MIN_SECONDS_LEFT",
                args.min_seconds_left or existing.get("QUANT_MIN_SECONDS_LEFT", "45"),
            )
            set_env_value(config_path, "QUANT_COOLDOWN_SEC", args.cooldown_sec or existing.get("QUANT_COOLDOWN_SEC", "60"))
            set_env_value(
                config_path,
                "QUANT_LOG_INTERVAL_SEC",
                args.log_interval_sec or existing.get("QUANT_LOG_INTERVAL_SEC", "10"),
            )
            print("[OK] AI量化参数已更新")
        if args.restart:
            local_service_action("restart")
    elif args.command == "quant-once":
        quant_once(Path(args.config))
    elif args.command == "install":
        install_from_git(
            host=args.host,
            user=args.user,
            remote_dir=args.remote_dir,
            config_path=Path(args.config),
            service_name=args.service_name,
            repo_url=args.repo_url,
            branch=args.branch,
            install_system_deps=not args.skip_system_deps,
        )
    elif args.command == "deploy":
        deploy_to_vps(
            host=args.host,
            user=args.user,
            remote_dir=args.remote_dir,
            config_path=Path(args.config),
            service_name=args.service_name,
            install_system_deps=not args.skip_system_deps,
        )
    elif args.command == "remote":
        remote_action(
            action=args.action,
            host=args.host,
            user=args.user,
            remote_dir=args.remote_dir,
            service_name=args.service_name,
            dry_run_value=args.value,
        )
    else:
        parser.print_help()
        raise SystemExit(1)


if __name__ == "__main__":
    main(sys.argv[1:])
