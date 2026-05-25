from __future__ import annotations

import argparse
import getpass
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import dotenv_values

from .bot import PolymarketCopyBot
from .config import (
    DEFAULT_CLOB_API_URL,
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


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def prompt_text(label: str, default: str = "", secret: bool = False) -> str:
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
        "SIGNATURE_TYPE": prompt_text(
            "签名类型 SIGNATURE_TYPE",
            existing.get("SIGNATURE_TYPE", "3"),
        ),
        "DEPOSIT_WALLET_ADDRESS": prompt_text(
            "你的 Deposit Wallet 地址",
            existing.get("DEPOSIT_WALLET_ADDRESS", ""),
        ),
        "POLL_SEC": prompt_text("轮询间隔秒数 POLL_SEC", existing.get("POLL_SEC", "1")),
        "COPY_RATIO": prompt_text("跟单比例 COPY_RATIO", existing.get("COPY_RATIO", "1.0")),
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
    }
    write_env(config_path, values)
    print(f"[OK] 已写入 {config_path}")


def print_config_summary(config_path: Path, require_private_key: Optional[bool] = None) -> bool:
    config = load_config(config_path)
    if require_private_key is None:
        require_private_key = not config.dry_run
    errors, warnings = validate_config(config, require_private_key=require_private_key)
    print(f"配置文件: {config_path}")
    print(f"目标: @{config.target_username} / {config.target_wallet}")
    print(f"模式: {config.mode_label}")
    print(f"跟单比例: {config.copy_ratio}")
    print(f"轮询间隔: {config.poll_sec}s")
    print(f"私钥: {config.masked_private_key}")
    print(f"Deposit Wallet: {config.deposit_wallet_address or '<empty>'}")
    for warning in warnings:
        print(f"[WARN] {warning}")
    for error in errors:
        print(f"[ERROR] {error}")
    if not errors:
        print("[OK] 配置校验通过")
    return not errors


def run_bot(config_path: Path) -> None:
    config = load_config(config_path)
    bot = PolymarketCopyBot(config)
    bot.run_forever()


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


def interactive_menu() -> None:
    config_path = Path(".env")
    while True:
        print("")
        print("JY Polymarket Copy CLI")
        print("1. 初始化/更新本地 .env")
        print("2. 校验本地配置")
        print("3. 本地运行机器人")
        print("4. 从 GitHub 安装到 VPS")
        print("5. 上传本地代码部署到 VPS")
        print("6. 更新远程程序")
        print("7. 查看远程服务状态")
        print("8. 查看远程实时日志")
        print("9. 重启远程服务")
        print("10. 切换远程 DRY_RUN")
        print("0. 退出")
        choice = input("请选择: ").strip()
        try:
            if choice == "1":
                init_config(config_path)
            elif choice == "2":
                print_config_summary(config_path)
            elif choice == "3":
                run_bot(config_path)
            elif choice == "4":
                host = prompt_text("VPS IP / Host")
                user = prompt_text("SSH 用户", "root")
                remote_dir = prompt_text("远程目录", DEFAULT_REMOTE_DIR)
                repo_url = prompt_text("GitHub 仓库地址", DEFAULT_REPO_URL)
                branch = prompt_text("Git 分支", DEFAULT_REPO_BRANCH)
                install_from_git(
                    host,
                    user,
                    remote_dir,
                    config_path,
                    DEFAULT_SERVICE,
                    repo_url,
                    branch,
                    True,
                )
            elif choice == "5":
                host = prompt_text("VPS IP / Host")
                user = prompt_text("SSH 用户", "root")
                remote_dir = prompt_text("远程目录", DEFAULT_REMOTE_DIR)
                deploy_to_vps(host, user, remote_dir, config_path, DEFAULT_SERVICE, True)
            elif choice == "6":
                host = prompt_text("VPS IP / Host")
                user = prompt_text("SSH 用户", "root")
                remote_action("update", host, user, DEFAULT_REMOTE_DIR, DEFAULT_SERVICE, None)
            elif choice == "7":
                host = prompt_text("VPS IP / Host")
                user = prompt_text("SSH 用户", "root")
                remote_action("status", host, user, DEFAULT_REMOTE_DIR, DEFAULT_SERVICE, None)
            elif choice == "8":
                host = prompt_text("VPS IP / Host")
                user = prompt_text("SSH 用户", "root")
                remote_action("logs", host, user, DEFAULT_REMOTE_DIR, DEFAULT_SERVICE, None)
            elif choice == "9":
                host = prompt_text("VPS IP / Host")
                user = prompt_text("SSH 用户", "root")
                remote_action("restart", host, user, DEFAULT_REMOTE_DIR, DEFAULT_SERVICE, None)
            elif choice == "10":
                host = prompt_text("VPS IP / Host")
                user = prompt_text("SSH 用户", "root")
                value = prompt_text("DRY_RUN 值，1=只打印，0=真实下单", "1")
                remote_action("dry-run", host, user, DEFAULT_REMOTE_DIR, DEFAULT_SERVICE, value)
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Interactive CLI for Polymarket copy bot.")
    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init-config", help="交互式生成 .env")
    p_init.add_argument("--config", default=".env")

    p_validate = sub.add_parser("validate", help="校验配置")
    p_validate.add_argument("--config", default=".env")

    p_run = sub.add_parser("run", help="本地运行机器人")
    p_run.add_argument("--config", default=".env")

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

    if args.command == "init-config":
        init_config(Path(args.config))
    elif args.command == "validate":
        ok = print_config_summary(Path(args.config))
        raise SystemExit(0 if ok else 1)
    elif args.command == "run":
        run_bot(Path(args.config))
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
