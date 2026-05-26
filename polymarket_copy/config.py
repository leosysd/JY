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
DEFAULT_POLYMARKET_RTDS_WS_URL = "wss://ws-live-data.polymarket.com"
DEFAULT_QUANT_CHAINLINK_TIMEOUT_SEC = "20"
DEFAULT_QUANT_CHAINLINK_MAX_AGE_SEC = "240"
DEFAULT_QUANT_CHAINLINK_START_TOLERANCE_SEC = "180"
DEFAULT_QUANT_ORDER_SHARES = "5"
DEFAULT_QUANT_MARKET_MAX_USDC = "60"
DEFAULT_QUANT_MAX_TRADES_PER_MARKET = "8"
DEFAULT_QUANT_REBUY_COOLDOWN_SEC = "15"
DEFAULT_QUANT_MAX_DRAWDOWN_USDC = "30"


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


def _env_upgraded_default(name: str, default: str, legacy_default: str) -> str:
    value = _env(name, default)
    return default if value == legacy_default else value


def _path_env(name: str, default: str, config_path: Optional[Path]) -> Path:
    value = Path(_env(name, default))
    if value.is_absolute() or config_path is None:
        return value
    return config_path.expanduser().resolve().parent / value


def is_eth_address(value: str) -> bool:
    return bool(re.fullmatch(r"0x[a-fA-F0-9]{40}", value.strip()))


def looks_like_private_key(value: str) -> bool:
    return bool(re.fullmatch(r"0x[a-fA-F0-9]{64}", value.strip()))


@dataclass(frozen=True)
class CopyBotConfig:
    bot_mode: str = "copy"
    target_username: str = DEFAULT_TARGET_USERNAME
    target_wallet: str = DEFAULT_TARGET_WALLET
    private_key: str = ""
    chain_id: int = 137
    clob_api_url: str = DEFAULT_CLOB_API_URL
    signature_type: int = 3
    deposit_wallet_address: str = ""
    poll_sec: float = 1.0
    copy_ratio: Decimal = Decimal("1.0")
    price_mode: str = "safe"
    max_slippage: Decimal = Decimal("0.02")
    max_order_usdc: Decimal = Decimal("0")
    dry_run: bool = True
    state_file: Path = Path("seen_jetfadil.json")
    activity_limit: int = 100
    bootstrap_limit: int = 500
    book_cache_sec: float = 300.0
    http_timeout_sec: float = 10.0
    http_max_retries: int = 5
    backoff_base_sec: float = 0.5
    backoff_max_sec: float = 20.0
    mark_failed_seen: bool = False
    enable_market_ws: bool = True
    market_ws_url: str = DEFAULT_MARKET_WS_URL
    market_ws_bootstrap_assets: int = 100
    market_ws_max_assets: int = 250
    market_ws_book_max_age_sec: float = 900.0
    market_ws_heartbeat_sec: float = 10.0
    market_ws_reconnect_sec: float = 3.0
    market_ws_custom_feature_enabled: bool = True
    quant_symbol: str = "BTC-USDT"
    quant_market_slug_prefix: str = "btc-updown-5m"
    quant_price_source: str = "chainlink"
    quant_chainlink_symbol: str = "btc/usd"
    quant_chainlink_ws_url: str = DEFAULT_POLYMARKET_RTDS_WS_URL
    quant_chainlink_timeout_sec: float = float(DEFAULT_QUANT_CHAINLINK_TIMEOUT_SEC)
    quant_chainlink_max_age_sec: float = float(DEFAULT_QUANT_CHAINLINK_MAX_AGE_SEC)
    quant_chainlink_start_tolerance_sec: float = float(DEFAULT_QUANT_CHAINLINK_START_TOLERANCE_SEC)
    quant_strategy: str = "single"
    quant_size_mode: str = "usdc"
    quant_order_usdc: Decimal = Decimal("5")
    quant_order_shares: Decimal = Decimal(DEFAULT_QUANT_ORDER_SHARES)
    quant_capital_usdc: Decimal = Decimal("300")
    quant_market_max_usdc: Decimal = Decimal(DEFAULT_QUANT_MARKET_MAX_USDC)
    quant_max_trades_per_market: int = int(DEFAULT_QUANT_MAX_TRADES_PER_MARKET)
    quant_rebuy_cooldown_sec: float = float(DEFAULT_QUANT_REBUY_COOLDOWN_SEC)
    quant_lock_min_profit: Decimal = Decimal("0.50")
    quant_lock_stop_on_lock: bool = True
    quant_min_edge: Decimal = Decimal("0.04")
    quant_min_seconds_left: int = 45
    quant_max_drawdown_usdc: Decimal = Decimal(DEFAULT_QUANT_MAX_DRAWDOWN_USDC)
    quant_cooldown_sec: float = 60.0
    quant_log_interval_sec: float = 10.0
    quant_state_file: Path = Path("quant_state.json")
    quant_record_signals: bool = True
    quant_signal_file: Path = Path("data/quant_signals.jsonl")
    quant_signal_interval_sec: float = 30.0
    log_to_file: bool = True
    log_file: Path = Path("logs/polymarket-copy.log")

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
    max_slippage = _env("MAX_SLIPPAGE")
    if not max_slippage:
        max_slippage_bps = _env("MAX_SLIPPAGE_BPS")
        max_slippage = str(Decimal(max_slippage_bps) / Decimal("10000")) if max_slippage_bps else "0.02"

    return CopyBotConfig(
        bot_mode=_env("BOT_MODE", "copy").lower(),
        target_username=target_username,
        target_wallet=_env("TARGET_WALLET", DEFAULT_TARGET_WALLET),
        private_key=_env("PRIVATE_KEY"),
        chain_id=int(_env("CHAIN_ID", "137")),
        clob_api_url=_env("CLOB_API_URL", DEFAULT_CLOB_API_URL),
        signature_type=int(_env("SIGNATURE_TYPE", "3")),
        deposit_wallet_address=funder,
        poll_sec=float(_env("POLL_SEC", "1")),
        copy_ratio=Decimal(_env("COPY_RATIO", "1.0")),
        price_mode=_env("PRICE_MODE", "safe").lower(),
        max_slippage=Decimal(max_slippage),
        max_order_usdc=Decimal(_env("MAX_ORDER_USDC", "0")),
        dry_run=parse_bool(_env("DRY_RUN", "1"), default=True),
        state_file=Path(_env("STATE_FILE", state_default)),
        activity_limit=int(_env("ACTIVITY_LIMIT", "100")),
        bootstrap_limit=int(_env("BOOTSTRAP_LIMIT", "500")),
        book_cache_sec=float(_env("BOOK_CACHE_SEC", "300")),
        http_timeout_sec=float(_env("HTTP_TIMEOUT_SEC", "10")),
        http_max_retries=int(_env("HTTP_MAX_RETRIES", "5")),
        backoff_base_sec=float(_env("BACKOFF_BASE_SEC", "0.5")),
        backoff_max_sec=float(_env("BACKOFF_MAX_SEC", "20")),
        mark_failed_seen=parse_bool(_env("MARK_FAILED_SEEN", "0"), default=False),
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
        quant_symbol=_env("QUANT_SYMBOL", "BTC-USDT"),
        quant_market_slug_prefix=_env("QUANT_MARKET_SLUG_PREFIX", "btc-updown-5m"),
        quant_price_source=_env("QUANT_PRICE_SOURCE", "chainlink").lower(),
        quant_chainlink_symbol=_env("QUANT_CHAINLINK_SYMBOL", "btc/usd").lower(),
        quant_chainlink_ws_url=_env("QUANT_CHAINLINK_WS_URL", DEFAULT_POLYMARKET_RTDS_WS_URL),
        quant_chainlink_timeout_sec=float(
            _env_upgraded_default(
                "QUANT_CHAINLINK_TIMEOUT_SEC",
                DEFAULT_QUANT_CHAINLINK_TIMEOUT_SEC,
                "12",
            )
        ),
        quant_chainlink_max_age_sec=float(
            _env_upgraded_default(
                "QUANT_CHAINLINK_MAX_AGE_SEC",
                DEFAULT_QUANT_CHAINLINK_MAX_AGE_SEC,
                "180",
            )
        ),
        quant_chainlink_start_tolerance_sec=float(
            _env_upgraded_default(
                "QUANT_CHAINLINK_START_TOLERANCE_SEC",
                DEFAULT_QUANT_CHAINLINK_START_TOLERANCE_SEC,
                "4",
            )
        ),
        quant_strategy=_env("QUANT_STRATEGY", "single").lower(),
        quant_size_mode=_env("QUANT_SIZE_MODE", "usdc").lower(),
        quant_order_usdc=Decimal(_env("QUANT_ORDER_USDC", "5")),
        quant_order_shares=Decimal(_env("QUANT_ORDER_SHARES", DEFAULT_QUANT_ORDER_SHARES)),
        quant_capital_usdc=Decimal(_env("QUANT_CAPITAL_USDC", "300")),
        quant_market_max_usdc=Decimal(_env("QUANT_MARKET_MAX_USDC", DEFAULT_QUANT_MARKET_MAX_USDC)),
        quant_max_trades_per_market=int(_env("QUANT_MAX_TRADES_PER_MARKET", DEFAULT_QUANT_MAX_TRADES_PER_MARKET)),
        quant_rebuy_cooldown_sec=float(_env("QUANT_REBUY_COOLDOWN_SEC", DEFAULT_QUANT_REBUY_COOLDOWN_SEC)),
        quant_lock_min_profit=Decimal(_env("QUANT_LOCK_MIN_PROFIT", "0.50")),
        quant_lock_stop_on_lock=parse_bool(_env("QUANT_LOCK_STOP_ON_LOCK", "1"), default=True),
        quant_min_edge=Decimal(_env("QUANT_MIN_EDGE", "0.04")),
        quant_min_seconds_left=int(_env("QUANT_MIN_SECONDS_LEFT", "45")),
        quant_max_drawdown_usdc=Decimal(_env("QUANT_MAX_DRAWDOWN_USDC", DEFAULT_QUANT_MAX_DRAWDOWN_USDC)),
        quant_cooldown_sec=float(_env("QUANT_COOLDOWN_SEC", "60")),
        quant_log_interval_sec=float(_env("QUANT_LOG_INTERVAL_SEC", "10")),
        quant_state_file=_path_env("QUANT_STATE_FILE", "quant_state.json", config_path),
        quant_record_signals=parse_bool(_env("QUANT_RECORD_SIGNALS", "1"), default=True),
        quant_signal_file=_path_env("QUANT_SIGNAL_FILE", "data/quant_signals.jsonl", config_path),
        quant_signal_interval_sec=float(_env("QUANT_SIGNAL_INTERVAL_SEC", "30")),
        log_to_file=parse_bool(_env("LOG_TO_FILE", "1"), default=True),
        log_file=_path_env("LOG_FILE", "logs/polymarket-copy.log", config_path),
    )


def validate_config(config: CopyBotConfig, require_private_key: bool = False) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []

    if config.bot_mode not in {"copy", "quant"}:
        errors.append("BOT_MODE 必须是 copy 或 quant")
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
    needs_trade_signing_config = require_private_key or not config.dry_run
    if config.signature_type == 3 and not config.deposit_wallet_address:
        if needs_trade_signing_config:
            errors.append("SIGNATURE_TYPE=3 时必须设置 DEPOSIT_WALLET_ADDRESS / FUNDER_ADDRESS")
        else:
            warnings.append("DRY_RUN=1 且未设置 DEPOSIT_WALLET_ADDRESS；可以模拟监控，但不能真实下单")
    if config.signature_type == 3 and config.deposit_wallet_address:
        if not is_eth_address(config.deposit_wallet_address):
            if needs_trade_signing_config:
                errors.append("DEPOSIT_WALLET_ADDRESS / FUNDER_ADDRESS 看起来不是合法 0x 钱包地址")
            else:
                warnings.append("DEPOSIT_WALLET_ADDRESS / FUNDER_ADDRESS 看起来不是合法 0x 钱包地址")
    if config.copy_ratio <= 0:
        errors.append("COPY_RATIO 必须大于 0")
    if config.price_mode not in {"safe", "aggressive"}:
        errors.append("PRICE_MODE 必须是 safe 或 aggressive")
    if config.max_slippage < 0:
        errors.append("MAX_SLIPPAGE 必须大于等于 0")
    if config.max_slippage >= 1:
        errors.append("MAX_SLIPPAGE 必须小于 1")
    if config.price_mode == "aggressive":
        warnings.append("PRICE_MODE=aggressive 会使用 0.99/0.01 激进限价，滑点风险很高")
    if config.max_order_usdc < 0:
        errors.append("MAX_ORDER_USDC 必须大于等于 0")
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
    if config.quant_price_source not in {"chainlink", "okx"}:
        errors.append("QUANT_PRICE_SOURCE 必须是 chainlink 或 okx")
    if config.quant_price_source == "chainlink" and config.quant_chainlink_symbol != "btc/usd":
        warnings.append("当前 AI量化第一版只验证过 Chainlink btc/usd")
    if config.quant_chainlink_timeout_sec <= 0:
        errors.append("QUANT_CHAINLINK_TIMEOUT_SEC 必须大于 0")
    if config.quant_chainlink_max_age_sec <= 0:
        errors.append("QUANT_CHAINLINK_MAX_AGE_SEC 必须大于 0")
    if config.quant_chainlink_start_tolerance_sec <= 0:
        errors.append("QUANT_CHAINLINK_START_TOLERANCE_SEC 必须大于 0")
    if config.quant_strategy not in {"single", "lock"}:
        errors.append("QUANT_STRATEGY 必须是 single 或 lock")
    if config.quant_strategy == "lock" and not config.dry_run:
        errors.append("QUANT_STRATEGY=lock 第一版只允许 DRY_RUN=1，确认模拟稳定后再接实盘")
    if config.quant_size_mode not in {"usdc", "shares"}:
        errors.append("QUANT_SIZE_MODE 必须是 usdc 或 shares")
    if config.quant_order_usdc <= 0:
        errors.append("QUANT_ORDER_USDC 必须大于 0")
    if config.quant_order_shares <= 0:
        errors.append("QUANT_ORDER_SHARES 必须大于 0")
    if config.quant_capital_usdc <= 0:
        errors.append("QUANT_CAPITAL_USDC 必须大于 0")
    if config.quant_market_max_usdc <= 0:
        errors.append("QUANT_MARKET_MAX_USDC 必须大于 0")
    if config.quant_market_max_usdc > config.quant_capital_usdc:
        warnings.append("QUANT_MARKET_MAX_USDC 大于 QUANT_CAPITAL_USDC，会按总本金上限保护")
    if config.quant_max_trades_per_market < 1:
        errors.append("QUANT_MAX_TRADES_PER_MARKET 至少为 1")
    if config.quant_rebuy_cooldown_sec < 0:
        errors.append("QUANT_REBUY_COOLDOWN_SEC 必须大于等于 0")
    if config.quant_lock_min_profit < 0:
        errors.append("QUANT_LOCK_MIN_PROFIT 必须大于等于 0")
    if config.quant_min_edge < 0:
        errors.append("QUANT_MIN_EDGE 必须大于等于 0")
    if config.quant_min_seconds_left < 0:
        errors.append("QUANT_MIN_SECONDS_LEFT 必须大于等于 0")
    if config.quant_max_drawdown_usdc < 0:
        errors.append("QUANT_MAX_DRAWDOWN_USDC 必须大于等于 0")
    if config.quant_cooldown_sec < 0:
        errors.append("QUANT_COOLDOWN_SEC 必须大于等于 0")
    if config.quant_log_interval_sec <= 0:
        errors.append("QUANT_LOG_INTERVAL_SEC 必须大于 0")
    if config.quant_signal_interval_sec <= 0:
        errors.append("QUANT_SIGNAL_INTERVAL_SEC 必须大于 0")
    return errors, warnings


def env_lines(values: Dict[str, str]) -> List[str]:
    ordered_keys: Iterable[str] = (
        "BOT_MODE",
        "TARGET_USERNAME",
        "TARGET_WALLET",
        "PRIVATE_KEY",
        "CHAIN_ID",
        "CLOB_API_URL",
        "SIGNATURE_TYPE",
        "DEPOSIT_WALLET_ADDRESS",
        "POLL_SEC",
        "COPY_RATIO",
        "PRICE_MODE",
        "MAX_SLIPPAGE",
        "MAX_ORDER_USDC",
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
        "QUANT_SYMBOL",
        "QUANT_MARKET_SLUG_PREFIX",
        "QUANT_PRICE_SOURCE",
        "QUANT_CHAINLINK_SYMBOL",
        "QUANT_CHAINLINK_WS_URL",
        "QUANT_CHAINLINK_TIMEOUT_SEC",
        "QUANT_CHAINLINK_MAX_AGE_SEC",
        "QUANT_CHAINLINK_START_TOLERANCE_SEC",
        "QUANT_STRATEGY",
        "QUANT_SIZE_MODE",
        "QUANT_ORDER_USDC",
        "QUANT_ORDER_SHARES",
        "QUANT_CAPITAL_USDC",
        "QUANT_MARKET_MAX_USDC",
        "QUANT_MAX_TRADES_PER_MARKET",
        "QUANT_REBUY_COOLDOWN_SEC",
        "QUANT_LOCK_MIN_PROFIT",
        "QUANT_LOCK_STOP_ON_LOCK",
        "QUANT_MIN_EDGE",
        "QUANT_MIN_SECONDS_LEFT",
        "QUANT_MAX_DRAWDOWN_USDC",
        "QUANT_COOLDOWN_SEC",
        "QUANT_LOG_INTERVAL_SEC",
        "QUANT_STATE_FILE",
        "QUANT_RECORD_SIGNALS",
        "QUANT_SIGNAL_FILE",
        "QUANT_SIGNAL_INTERVAL_SEC",
        "LOG_TO_FILE",
        "LOG_FILE",
    )
    return [f"{key}={values.get(key, '')}" for key in ordered_keys]
