from __future__ import annotations

import os
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from dotenv import load_dotenv


DEFAULT_TARGET_USERNAME = "jetfadil"
DEFAULT_TARGET_WALLET = "0xe0229e10a858860218b6132f4234602c47bd6603"
DEFAULT_CLOB_API_URL = "https://clob.polymarket.com"
DEFAULT_MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def parse_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def is_eth_address(value: str) -> bool:
    return bool(re.fullmatch(r"0x[a-fA-F0-9]{40}", value.strip()))


def looks_like_private_key(value: str) -> bool:
    return bool(re.fullmatch(r"0x[a-fA-F0-9]{64}", value.strip()))


@dataclass(frozen=True)
class CopyBotConfig:
    target_username: str = DEFAULT_TARGET_USERNAME
    target_wallet: str = DEFAULT_TARGET_WALLET
    private_key: str = ""
    chain_id: int = 137
    clob_api_url: str = DEFAULT_CLOB_API_URL
    signature_type: int = 3
    deposit_wallet_address: str = ""
    poll_sec: float = 1.0
    copy_ratio: Decimal = Decimal("1.0")
    dry_run: bool = True
    state_file: Path = Path("seen_jetfadil.json")
    activity_limit: int = 100
    bootstrap_limit: int = 500
    book_cache_sec: float = 300.0
    http_timeout_sec: float = 10.0
    http_max_retries: int = 5
    backoff_base_sec: float = 0.5
    backoff_max_sec: float = 20.0
    mark_failed_seen: bool = True
    enable_market_ws: bool = True
    market_ws_url: str = DEFAULT_MARKET_WS_URL
    market_ws_bootstrap_assets: int = 100
    market_ws_max_assets: int = 250
    market_ws_book_max_age_sec: float = 900.0
    market_ws_heartbeat_sec: float = 10.0
    market_ws_reconnect_sec: float = 3.0
    market_ws_custom_feature_enabled: bool = True

    @property
    def funder(self) -> str:
        return self.deposit_wallet_address

    @property
    def masked_private_key(self) -> str:
        if not self.private_key:
            return "<empty>"
        if len(self.private_key) <= 12:
            return "<set>"
        return f"{self.private_key[:6]}...{self.private_key[-4:]}"

    @property
    def mode_label(self) -> str:
        return "DRY_RUN" if self.dry_run else "LIVE"


def load_config(config_path: Optional[Path] = None) -> CopyBotConfig:
    if config_path is not None:
        load_dotenv(config_path, override=True)
    else:
        load_dotenv(override=True)

    target_username = _env("TARGET_USERNAME", DEFAULT_TARGET_USERNAME).lstrip("@")
    state_default = f"seen_{target_username}.json"
    funder = _env("DEPOSIT_WALLET_ADDRESS") or _env("FUNDER_ADDRESS") or _env("FUNDER")

    return CopyBotConfig(
        target_username=target_username,
        target_wallet=_env("TARGET_WALLET", DEFAULT_TARGET_WALLET),
        private_key=_env("PRIVATE_KEY"),
        chain_id=int(_env("CHAIN_ID", "137")),
        clob_api_url=_env("CLOB_API_URL", DEFAULT_CLOB_API_URL),
        signature_type=int(_env("SIGNATURE_TYPE", "3")),
        deposit_wallet_address=funder,
        poll_sec=float(_env("POLL_SEC", "1")),
        copy_ratio=Decimal(_env("COPY_RATIO", "1.0")),
        dry_run=parse_bool(_env("DRY_RUN", "1"), default=True),
        state_file=Path(_env("STATE_FILE", state_default)),
        activity_limit=int(_env("ACTIVITY_LIMIT", "100")),
        bootstrap_limit=int(_env("BOOTSTRAP_LIMIT", "500")),
        book_cache_sec=float(_env("BOOK_CACHE_SEC", "300")),
        http_timeout_sec=float(_env("HTTP_TIMEOUT_SEC", "10")),
        http_max_retries=int(_env("HTTP_MAX_RETRIES", "5")),
        backoff_base_sec=float(_env("BACKOFF_BASE_SEC", "0.5")),
        backoff_max_sec=float(_env("BACKOFF_MAX_SEC", "20")),
        mark_failed_seen=parse_bool(_env("MARK_FAILED_SEEN", "1"), default=True),
        enable_market_ws=parse_bool(_env("ENABLE_MARKET_WS", "1"), default=True),
        market_ws_url=_env("MARKET_WS_URL", DEFAULT_MARKET_WS_URL),
        market_ws_bootstrap_assets=int(_env("MARKET_WS_BOOTSTRAP_ASSETS", "100")),
        market_ws_max_assets=int(_env("MARKET_WS_MAX_ASSETS", "250")),
        market_ws_book_max_age_sec=float(_env("MARKET_WS_BOOK_MAX_AGE_SEC", "900")),
        market_ws_heartbeat_sec=float(_env("MARKET_WS_HEARTBEAT_SEC", "10")),
        market_ws_reconnect_sec=float(_env("MARKET_WS_RECONNECT_SEC", "3")),
        market_ws_custom_feature_enabled=parse_bool(
            _env("MARKET_WS_CUSTOM_FEATURE_ENABLED", "1"),
            default=True,
        ),
    )


def validate_config(config: CopyBotConfig, require_private_key: bool = False) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []

    if not config.target_username:
        errors.append("TARGET_USERNAME 不能为空")
    if config.target_wallet and not is_eth_address(config.target_wallet):
        warnings.append("TARGET_WALLET 看起来不是合法 0x 钱包地址")
    if require_private_key and not config.private_key:
        errors.append("PRIVATE_KEY 未设置")
    if require_private_key and config.private_key and not looks_like_private_key(config.private_key):
        errors.append("PRIVATE_KEY 看起来不是 64 字节十六进制私钥")
    elif config.private_key and not looks_like_private_key(config.private_key):
        warnings.append("PRIVATE_KEY 看起来不是 64 字节十六进制私钥")
    if config.signature_type == 3 and not config.deposit_wallet_address:
        errors.append("SIGNATURE_TYPE=3 时必须设置 DEPOSIT_WALLET_ADDRESS / FUNDER_ADDRESS")
    if config.signature_type == 3 and config.deposit_wallet_address:
        if not is_eth_address(config.deposit_wallet_address):
            errors.append("DEPOSIT_WALLET_ADDRESS / FUNDER_ADDRESS 看起来不是合法 0x 钱包地址")
    if config.copy_ratio <= 0:
        errors.append("COPY_RATIO 必须大于 0")
    if config.poll_sec <= 0:
        errors.append("POLL_SEC 必须大于 0")
    if config.poll_sec < 0.5:
        warnings.append("POLL_SEC 小于 0.5，可能更容易触发限速")
    if config.activity_limit <= 0:
        errors.append("ACTIVITY_LIMIT 必须大于 0")
    if config.http_max_retries < 1:
        errors.append("HTTP_MAX_RETRIES 至少为 1")
    if config.market_ws_max_assets < 1:
        errors.append("MARKET_WS_MAX_ASSETS 至少为 1")
    if config.market_ws_heartbeat_sec <= 0:
        errors.append("MARKET_WS_HEARTBEAT_SEC 必须大于 0")
    return errors, warnings


def env_lines(values: Dict[str, str]) -> List[str]:
    ordered_keys: Iterable[str] = (
        "TARGET_USERNAME",
        "TARGET_WALLET",
        "PRIVATE_KEY",
        "CHAIN_ID",
        "CLOB_API_URL",
        "SIGNATURE_TYPE",
        "DEPOSIT_WALLET_ADDRESS",
        "POLL_SEC",
        "COPY_RATIO",
        "DRY_RUN",
        "STATE_FILE",
        "ACTIVITY_LIMIT",
        "BOOTSTRAP_LIMIT",
        "BOOK_CACHE_SEC",
        "HTTP_TIMEOUT_SEC",
        "HTTP_MAX_RETRIES",
        "BACKOFF_BASE_SEC",
        "BACKOFF_MAX_SEC",
        "MARK_FAILED_SEEN",
        "ENABLE_MARKET_WS",
        "MARKET_WS_URL",
        "MARKET_WS_BOOTSTRAP_ASSETS",
        "MARKET_WS_MAX_ASSETS",
        "MARKET_WS_BOOK_MAX_AGE_SEC",
        "MARKET_WS_HEARTBEAT_SEC",
        "MARKET_WS_RECONNECT_SEC",
        "MARKET_WS_CUSTOM_FEATURE_ENABLED",
    )
    return [f"{key}={values.get(key, '')}" for key in ordered_keys]
