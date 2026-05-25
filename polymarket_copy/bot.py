from __future__ import annotations

import argparse
import contextlib
import io
import json
import random
import re
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, getcontext
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import requests

from .config import CopyBotConfig, load_config, validate_config
from .logging_utils import setup_file_logging


getcontext().prec = 28

GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"


def decimal_value(value: Any) -> Decimal:
    return Decimal(str(value))


def now_ts() -> float:
    return time.time()


def is_non_retryable_http_error(exc: requests.RequestException) -> bool:
    response = getattr(exc, "response", None)
    if response is None:
        return False
    status = response.status_code
    return 400 <= status < 500 and status != 429


def is_missing_order_book_error(exc: BaseException) -> bool:
    response = getattr(exc, "response", None)
    url = getattr(response, "url", "") if response is not None else ""
    status = getattr(response, "status_code", None) if response is not None else None
    text = f"{repr(exc)} {url}"
    return (status == 404 or "404 Client Error" in text) and "/book" in text and "token_id=" in text


class HttpJsonClient:
    def __init__(self, config: CopyBotConfig) -> None:
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "jy-polymarket-copy/0.1"})

    def get_json(self, url: str, params: Optional[Dict[str, Any]] = None) -> Any:
        last_error: Optional[BaseException] = None
        for attempt in range(self.config.http_max_retries):
            try:
                response = self.session.get(
                    url,
                    params=params,
                    timeout=self.config.http_timeout_sec,
                )
                if response.status_code in {429, 500, 502, 503, 504}:
                    wait_sec = self._wait_seconds(response, attempt)
                    print(
                        f"[HTTP RETRY] status={response.status_code} "
                        f"attempt={attempt + 1}/{self.config.http_max_retries} "
                        f"sleep={wait_sec:.2f}s url={url}"
                    )
                    time.sleep(wait_sec)
                    continue
                response.raise_for_status()
                return response.json()
            except requests.RequestException as exc:
                last_error = exc
                if is_non_retryable_http_error(exc):
                    status = exc.response.status_code if exc.response is not None else "unknown"
                    print(f"[HTTP ERROR] non-retryable status={status} error={repr(exc)}")
                    raise
                if attempt == self.config.http_max_retries - 1:
                    break
                wait_sec = self._backoff_seconds(attempt)
                print(
                    f"[HTTP ERROR] attempt={attempt + 1}/{self.config.http_max_retries} "
                    f"sleep={wait_sec:.2f}s error={repr(exc)}"
                )
                time.sleep(wait_sec)
        if last_error is not None:
            raise last_error
        raise RuntimeError(f"HTTP request failed after retries: {url}")

    def _wait_seconds(self, response: requests.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), self.config.backoff_max_sec)
            except ValueError:
                pass
        return self._backoff_seconds(attempt)

    def _backoff_seconds(self, attempt: int) -> float:
        base = self.config.backoff_base_sec * (2 ** attempt)
        jitter = random.uniform(0, self.config.backoff_base_sec)
        return min(base + jitter, self.config.backoff_max_sec)


class SeenStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.state: Dict[str, Any] = {"seen": []}

    def load(self) -> None:
        if not self.path.exists():
            self.state = {"seen": []}
            return
        try:
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(self.state.get("seen"), list):
                self.state["seen"] = []
        except json.JSONDecodeError:
            backup = self.path.with_suffix(self.path.suffix + ".bad")
            self.path.replace(backup)
            print(f"[WARN] state 文件 JSON 无效，已备份到 {backup}")
            self.state = {"seen": []}

    def seen_set(self) -> Set[str]:
        return set(str(item) for item in self.state.get("seen", []))

    def save(self, seen: Set[str]) -> None:
        self.state["seen"] = list(seen)[-10000:]
        self.path.write_text(
            json.dumps(self.state, indent=2, sort_keys=True, ensure_ascii=False),
            encoding="utf-8",
        )


@dataclass
class BookCacheEntry:
    loaded_at: float
    book: Dict[str, Any]


@dataclass(frozen=True)
class PriceDecision:
    price: Decimal
    best_price: Optional[Decimal]
    best_label: str
    reason: str


class BookCache:
    def __init__(
        self,
        http: HttpJsonClient,
        config: CopyBotConfig,
        ws_cache: Optional["MarketWsBookCache"] = None,
    ) -> None:
        self.http = http
        self.config = config
        self.ws_cache = ws_cache
        self.entries: Dict[str, BookCacheEntry] = {}

    def get_book(self, token_id: str) -> Dict[str, Any]:
        if self.ws_cache:
            self.ws_cache.subscribe_assets([token_id])
            ws_book = self.ws_cache.get_book(token_id)
            if ws_book:
                return ws_book

        entry = self.entries.get(token_id)
        if entry and now_ts() - entry.loaded_at <= self.config.book_cache_sec:
            return entry.book
        book = self.http.get_json(
            f"{self.config.clob_api_url}/book",
            params={"token_id": token_id},
        )
        self.entries[token_id] = BookCacheEntry(loaded_at=now_ts(), book=book)
        if self.ws_cache:
            self.ws_cache.remember_book(token_id, book)
        return book


class MarketWsBookCache:
    def __init__(self, config: CopyBotConfig) -> None:
        self.config = config
        self.assets: Set[str] = set()
        self.pending_assets: Set[str] = set()
        self.books: Dict[str, BookCacheEntry] = {}
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.ws: Any = None

    def start(self, asset_ids: Iterable[str]) -> None:
        if not self.config.enable_market_ws:
            print("[WS] Market WebSocket 已关闭，使用 HTTP /book 回退。")
            return
        self.subscribe_assets(asset_ids)
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._run, name="market-ws-cache", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        with self.lock:
            ws = self.ws
        if ws:
            try:
                ws.close()
            except Exception:
                pass

    def subscribe_assets(self, asset_ids: Iterable[str]) -> None:
        clean_assets = [str(asset_id) for asset_id in asset_ids if str(asset_id)]
        if not clean_assets or not self.config.enable_market_ws:
            return
        with self.lock:
            for asset_id in clean_assets:
                if asset_id in self.assets:
                    continue
                if len(self.assets) >= self.config.market_ws_max_assets:
                    print(f"[WS WARN] 订阅资产数量已达上限 {self.config.market_ws_max_assets}，跳过 {asset_id}")
                    continue
                self.assets.add(asset_id)
                self.pending_assets.add(asset_id)

    def get_book(self, token_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            entry = self.books.get(token_id)
        if not entry:
            return None
        if now_ts() - entry.loaded_at > self.config.market_ws_book_max_age_sec:
            return None
        if "tick_size" not in entry.book or "min_order_size" not in entry.book:
            return None
        return dict(entry.book)

    def remember_book(self, token_id: str, book: Dict[str, Any]) -> None:
        with self.lock:
            self.books[token_id] = BookCacheEntry(loaded_at=now_ts(), book=dict(book))

    def _run(self) -> None:
        try:
            import websocket  # type: ignore
        except ImportError:
            print("[WS WARN] 缺少 websocket-client，Market WS 缓存不可用。")
            return

        while not self.stop_event.is_set():
            ws = None
            try:
                with self.lock:
                    initial_assets = sorted(self.assets)
                    self.pending_assets.clear()
                ws = websocket.create_connection(self.config.market_ws_url, timeout=10)
                with self.lock:
                    self.ws = ws
                if initial_assets:
                    self._send_subscription(ws, initial_assets, initial=True)
                    print(f"[WS] 已连接 Market WebSocket，订阅 {len(initial_assets)} 个 asset。")
                else:
                    print("[WS] 已连接 Market WebSocket，等待 asset 订阅。")

                next_ping = time.monotonic() + self.config.market_ws_heartbeat_sec
                ws.settimeout(1)
                while not self.stop_event.is_set():
                    if time.monotonic() >= next_ping:
                        ws.send("PING")
                        next_ping = time.monotonic() + self.config.market_ws_heartbeat_sec

                    pending = self._take_pending_assets()
                    if pending:
                        self._send_subscription(ws, pending, initial=False)
                        print(f"[WS] 新增订阅 {len(pending)} 个 asset，总计 {len(self.assets)}。")

                    try:
                        message = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not message:
                        continue
                    if message == "PONG":
                        continue
                    if message == "PING":
                        ws.send("PONG")
                        continue
                    self._handle_message(str(message))
            except Exception as exc:
                if not self.stop_event.is_set():
                    print(f"[WS WARN] Market WebSocket 断开: {repr(exc)}，稍后重连。")
                    time.sleep(self.config.market_ws_reconnect_sec)
            finally:
                with self.lock:
                    self.ws = None
                if ws:
                    try:
                        ws.close()
                    except Exception:
                        pass

    def _take_pending_assets(self) -> List[str]:
        with self.lock:
            pending = sorted(self.pending_assets)
            self.pending_assets.clear()
        return pending

    def _send_subscription(self, ws: Any, asset_ids: List[str], initial: bool) -> None:
        payload: Dict[str, Any] = {
            "assets_ids": asset_ids,
            "custom_feature_enabled": self.config.market_ws_custom_feature_enabled,
        }
        if initial:
            payload["type"] = "market"
        else:
            payload["operation"] = "subscribe"
        ws.send(json.dumps(payload))

    def _handle_message(self, message: str) -> None:
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    self._handle_event(item)
        elif isinstance(data, dict):
            self._handle_event(data)

    def _handle_event(self, event: Dict[str, Any]) -> None:
        event_type = event.get("event_type")
        token_id = str(event.get("asset_id") or event.get("asset") or "")
        if not token_id:
            return

        if event_type == "book":
            book = dict(event)
            with self.lock:
                existing = self.books.get(token_id)
                if existing:
                    if "min_order_size" not in book and "min_order_size" in existing.book:
                        book["min_order_size"] = existing.book["min_order_size"]
                    if "neg_risk" not in book and "neg_risk" in existing.book:
                        book["neg_risk"] = existing.book["neg_risk"]
                    if "tick_size" not in book and "tick_size" in existing.book:
                        book["tick_size"] = existing.book["tick_size"]
                if "tick_size" not in book:
                    book["tick_size"] = "0.01"
                self.books[token_id] = BookCacheEntry(loaded_at=now_ts(), book=book)
            return

        if event_type == "tick_size_change":
            with self.lock:
                entry = self.books.get(token_id)
                if not entry:
                    return
                book = dict(entry.book)
                book["tick_size"] = str(event.get("new_tick_size") or book.get("tick_size", "0.01"))
                self.books[token_id] = BookCacheEntry(loaded_at=now_ts(), book=book)


class PolymarketCopyBot:
    def __init__(self, config: CopyBotConfig) -> None:
        self.config = config
        self.http = HttpJsonClient(config)
        self.market_ws = MarketWsBookCache(config)
        self.book_cache = BookCache(self.http, config, self.market_ws)
        self.store = SeenStore(config.state_file)
        self._order_lib: Optional[Dict[str, Any]] = None

    def run_forever(self) -> None:
        errors, warnings = validate_config(self.config, require_private_key=not self.config.dry_run)
        for warning in warnings:
            print(f"[CONFIG WARN] {warning}")
        if errors:
            raise RuntimeError("; ".join(errors))

        wallet = self.resolve_target_wallet()
        print(f"[TARGET] @{self.config.target_username} proxyWallet = {wallet}")

        self.store.load()
        bootstrap_history = self.bootstrap_seen(wallet)
        initial_assets = collect_asset_ids(bootstrap_history, self.config.market_ws_bootstrap_assets)
        if not initial_assets:
            recent_activities = self.fetch_activity(wallet, limit=self.config.activity_limit)
            initial_assets = collect_asset_ids(recent_activities, self.config.market_ws_bootstrap_assets)
        self.market_ws.start(initial_assets)
        seen = self.store.seen_set()

        client = None
        if self.config.dry_run:
            print("[MODE] DRY_RUN=1，只打印，不真实下单。真实跟单改 DRY_RUN=0。")
        else:
            client = self.build_client()
            print("[MODE] DRY_RUN=0，真实下单。")

        try:
            while True:
                try:
                    activities = self.fetch_activity(wallet, limit=self.config.activity_limit)
                    self.market_ws.subscribe_assets(collect_asset_ids(activities, self.config.market_ws_bootstrap_assets))
                    new_trades = self.find_new_trades(activities, seen)
                    for trade in new_trades:
                        key = trade_key(trade)
                        try:
                            self.place_copy_order(client, trade)
                        except Exception as exc:
                            print(f"[COPY ERROR] key={key} error={repr(exc)} trade={trade}")
                            if is_missing_order_book_error(exc):
                                print("[COPY WARN] CLOB /book 返回 404，本笔订单簿不可用，已标记 seen，避免旧单无限重试。")
                            elif not self.config.mark_failed_seen:
                                print("[COPY RETRY] MARK_FAILED_SEEN=0，本笔不标记 seen，下轮会继续尝试。")
                                continue
                            else:
                                print("[COPY WARN] MARK_FAILED_SEEN=1，本笔失败后仍标记 seen，避免重复下单。")
                        seen.add(key)
                        self.store.save(seen)
                    time.sleep(self.config.poll_sec)
                except KeyboardInterrupt:
                    print("退出。")
                    self.store.save(seen)
                    break
                except Exception as exc:
                    print(f"[LOOP ERROR] {repr(exc)}")
                    self.store.save(seen)
                    time.sleep(max(self.config.poll_sec, 3))
        finally:
            self.market_ws.stop()

    def resolve_target_wallet(self) -> str:
        wallet = self.config.target_wallet.strip()
        if wallet and re.fullmatch(r"0x[a-fA-F0-9]{40}", wallet):
            return wallet

        data = self.http.get_json(
            f"{GAMMA_API}/public-search",
            params={
                "q": self.config.target_username,
                "search_profiles": "true",
                "limit_per_type": 10,
            },
        )
        profiles = data.get("profiles") or []
        for profile in profiles:
            name = str(profile.get("name") or "").lower()
            pseudonym = str(profile.get("pseudonym") or "").lower()
            found_wallet = profile.get("proxyWallet")
            if found_wallet and (
                name == self.config.target_username.lower()
                or pseudonym == self.config.target_username.lower()
            ):
                return str(found_wallet)
        for profile in profiles:
            found_wallet = profile.get("proxyWallet")
            if found_wallet:
                print(
                    f"[WARN] 未精确匹配 @{self.config.target_username}，"
                    f"使用搜索到的地址: {found_wallet}"
                )
                return str(found_wallet)
        raise RuntimeError(f"找不到 @{self.config.target_username} 的 proxyWallet")

    def fetch_activity(self, wallet: str, limit: int) -> List[Dict[str, Any]]:
        data = self.http.get_json(
            f"{DATA_API}/activity",
            params={
                "user": wallet,
                "type": "TRADE",
                "limit": limit,
                "sortBy": "TIMESTAMP",
                "sortDirection": "DESC",
            },
        )
        if not isinstance(data, list):
            raise RuntimeError(f"activity 返回异常: {data}")
        return data

    def bootstrap_seen(self, wallet: str) -> List[Dict[str, Any]]:
        seen = self.store.seen_set()
        if seen:
            return []
        history = self.fetch_activity(wallet, limit=self.config.bootstrap_limit)
        for trade in history:
            seen.add(trade_key(trade))
        self.store.save(seen)
        print(f"[BOOTSTRAP] 已把启动前 {len(seen)} 条历史 TRADE 标记为 seen，不复制旧单。")
        return history

    def find_new_trades(
        self,
        activities: List[Dict[str, Any]],
        seen: Set[str],
    ) -> List[Dict[str, Any]]:
        new_trades: List[Dict[str, Any]] = []
        for trade in reversed(activities):
            key = trade_key(trade)
            if key not in seen:
                new_trades.append(trade)
        return new_trades

    def build_client(self) -> Any:
        order_lib = self._load_order_lib()
        if not self.config.private_key:
            raise RuntimeError("PRIVATE_KEY 未设置")
        if self.config.signature_type == 3 and not self.config.funder:
            raise RuntimeError("SIGNATURE_TYPE=3 时必须设置 DEPOSIT_WALLET_ADDRESS")

        clob_client = order_lib["ClobClient"]
        temp_client = clob_client(
            self.config.clob_api_url,
            key=self.config.private_key,
            chain_id=self.config.chain_id,
        )
        sdk_output = io.StringIO()
        try:
            with contextlib.redirect_stdout(sdk_output), contextlib.redirect_stderr(sdk_output):
                api_creds = temp_client.create_or_derive_api_key()
        except Exception:
            captured = sdk_output.getvalue().strip()
            if captured:
                print(f"[SDK OUTPUT] {captured}")
            raise
        captured = sdk_output.getvalue().strip()
        if captured:
            if "Could not create api key" in captured or "/auth/api-key" in captured:
                print("[INFO] SDK auth/api-key 提示已忽略，继续使用可用的 CLOB 凭证。")
            else:
                print(f"[SDK OUTPUT] {captured}")
        kwargs: Dict[str, Any] = {
            "host": self.config.clob_api_url,
            "key": self.config.private_key,
            "chain_id": self.config.chain_id,
            "creds": api_creds,
            "signature_type": self.config.signature_type,
        }
        if self.config.funder:
            kwargs["funder"] = self.config.funder
        return clob_client(**kwargs)

    def place_copy_order(self, client: Any, trade: Dict[str, Any]) -> None:
        side_raw = str(trade.get("side", "")).upper()
        if side_raw not in {"BUY", "SELL"}:
            print(f"[SKIP] 未知 side: {side_raw}")
            return

        token_id = str(trade["asset"])
        self.market_ws.subscribe_assets([token_id])
        target_size = decimal_value(trade["size"])
        copy_size = target_size * self.config.copy_ratio
        book = self.book_cache.get_book(token_id)
        tick_size = decimal_value(book.get("tick_size", "0.01"))
        min_order_size = decimal_value(book.get("min_order_size", "1"))
        neg_risk = bool(book.get("neg_risk", False))

        if copy_size < min_order_size:
            print(
                "[SKIP] 低于最小下单 size: "
                f"copy_size={copy_size}, min_order_size={min_order_size}, "
                f"title={trade.get('title')}"
            )
            return

        target_price = decimal_value(trade.get("price"))
        decision = choose_copy_price(
            side_raw,
            target_price,
            book,
            tick_size,
            self.config.price_mode,
            self.config.max_slippage,
        )
        if decision is None:
            print(
                "[SKIP] price protection: "
                f"side={side_raw} title={trade.get('title')} "
                f"outcome={trade.get('outcome')} target_price={target_price} "
                f"max_slippage={self.config.max_slippage}"
            )
            return

        price = decision.price
        capped_size = apply_max_order_usdc(copy_size, price, self.config.max_order_usdc)
        if capped_size != copy_size:
            print(
                "[CAP] MAX_ORDER_USDC: "
                f"copy_size={copy_size} -> {capped_size}, "
                f"price={price}, max_order_usdc={self.config.max_order_usdc}"
            )
            copy_size = capped_size
            if copy_size < min_order_size:
                print(
                    "[SKIP] MAX_ORDER_USDC below min_order_size: "
                    f"copy_size={copy_size}, min_order_size={min_order_size}, "
                    f"title={trade.get('title')}"
                )
                return

        best_text = decision.best_price if decision.best_price is not None else "n/a"
        print(
            f"[COPY {side_raw}] "
            f"title={trade.get('title')} | "
            f"outcome={trade.get('outcome')} | "
            f"asset={token_id} | "
            f"target_size={target_size} | "
            f"copy_size={copy_size} | "
            f"target_price={target_price} | "
            f"{decision.best_label}={best_text} | "
            f"copy_limit_price={price} | "
            f"price_mode={self.config.price_mode}"
        )

        if self.config.dry_run:
            return
        self._post_order(client, token_id, price, copy_size, side_raw, tick_size, neg_risk)

    def _post_order(
        self,
        client: Any,
        token_id: str,
        price: Decimal,
        copy_size: Decimal,
        side_raw: str,
        tick_size: Decimal,
        neg_risk: bool,
    ) -> None:
        order_lib = self._load_order_lib()
        side = order_lib["BUY"] if side_raw == "BUY" else order_lib["SELL"]
        response = client.create_and_post_order(
            order_lib["OrderArgs"](
                token_id=token_id,
                price=float(price),
                size=float(copy_size),
                side=side,
            ),
            options=order_lib["PartialCreateOrderOptions"](
                tick_size=str(tick_size),
                neg_risk=neg_risk,
            ),
            order_type=order_lib["OrderType"].GTC,
        )
        print(f"[ORDER RESP] {response}")

    def _load_order_lib(self) -> Dict[str, Any]:
        if self._order_lib is not None:
            return self._order_lib
        try:
            from py_clob_client_v2 import (  # type: ignore
                ClobClient,
                OrderArgs,
                OrderType,
                PartialCreateOrderOptions,
            )
            from py_clob_client_v2.order_builder.constants import BUY, SELL  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "缺少 py-clob-client-v2，请先运行 pip install -r requirements.txt"
            ) from exc
        self._order_lib = {
            "ClobClient": ClobClient,
            "OrderArgs": OrderArgs,
            "OrderType": OrderType,
            "PartialCreateOrderOptions": PartialCreateOrderOptions,
            "BUY": BUY,
            "SELL": SELL,
        }
        return self._order_lib


def run_configured_bot(config: CopyBotConfig) -> None:
    setup_file_logging(config.log_file, enabled=config.log_to_file)
    if config.bot_mode == "quant":
        from .quant import PolymarketQuantBot

        PolymarketQuantBot(config).run_forever()
        return
    PolymarketCopyBot(config).run_forever()


def trade_key(trade: Dict[str, Any]) -> str:
    parts = [
        str(trade.get("transactionHash", "")),
        str(trade.get("timestamp", "")),
        str(trade.get("asset", "")),
        str(trade.get("side", "")),
        str(trade.get("price", "")),
        str(trade.get("size", "")),
    ]
    return "|".join(parts)


def collect_asset_ids(trades: Iterable[Dict[str, Any]], limit: int) -> List[str]:
    assets: List[str] = []
    seen: Set[str] = set()
    for trade in trades:
        asset = str(trade.get("asset") or "")
        if not asset or asset in seen:
            continue
        seen.add(asset)
        assets.append(asset)
        if len(assets) >= limit:
            break
    return assets


def aggressive_price(side: str, tick_size: Decimal) -> Decimal:
    if side == "BUY":
        return Decimal("1") - tick_size
    return tick_size


def choose_copy_price(
    side: str,
    target_price: Decimal,
    book: Dict[str, Any],
    tick_size: Decimal,
    price_mode: str,
    max_slippage: Decimal,
) -> Optional[PriceDecision]:
    if price_mode == "aggressive":
        return PriceDecision(
            price=aggressive_price(side, tick_size),
            best_price=best_book_price(book, side),
            best_label=best_price_label(side),
            reason="aggressive",
        )

    safe_price = protected_limit_price(side, target_price, tick_size, max_slippage)
    best_price = best_book_price(book, side)
    best_label = best_price_label(side)
    if best_price is None:
        return None
    if side == "BUY" and best_price > safe_price:
        return None
    if side == "SELL" and best_price < safe_price:
        return None
    return PriceDecision(
        price=safe_price,
        best_price=best_price,
        best_label=best_label,
        reason="safe",
    )


def protected_limit_price(
    side: str,
    target_price: Decimal,
    tick_size: Decimal,
    max_slippage: Decimal,
) -> Decimal:
    if side == "BUY":
        raw_price = target_price + max_slippage
        rounded = round_price_to_tick(raw_price, tick_size, ROUND_CEILING)
        return min(max(rounded, tick_size), Decimal("1") - tick_size)
    raw_price = target_price - max_slippage
    rounded = round_price_to_tick(raw_price, tick_size, ROUND_FLOOR)
    return min(max(rounded, tick_size), Decimal("1") - tick_size)


def round_price_to_tick(price: Decimal, tick_size: Decimal, rounding: str) -> Decimal:
    if tick_size <= 0:
        tick_size = Decimal("0.01")
    ticks = (price / tick_size).to_integral_value(rounding=rounding)
    return ticks * tick_size


def best_price_label(side: str) -> str:
    return "best_ask" if side == "BUY" else "best_bid"


def best_book_price(book: Dict[str, Any], side: str) -> Optional[Decimal]:
    levels = book.get("asks") if side == "BUY" else book.get("bids")
    if not isinstance(levels, list) or not levels:
        return None
    prices: List[Decimal] = []
    for level in levels:
        if not isinstance(level, dict) or level.get("price") in {None, ""}:
            continue
        try:
            prices.append(decimal_value(level["price"]))
        except Exception:
            continue
    if not prices:
        return None
    return min(prices) if side == "BUY" else max(prices)


def apply_max_order_usdc(copy_size: Decimal, price: Decimal, max_order_usdc: Decimal) -> Decimal:
    if max_order_usdc <= 0 or price <= 0:
        return copy_size
    estimated_usdc = copy_size * price
    if estimated_usdc <= max_order_usdc:
        return copy_size
    return (max_order_usdc / price).quantize(Decimal("0.000001"), rounding=ROUND_FLOOR)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Polymarket realtime copy bot.")
    parser.add_argument("--config", default=".env", help="Path to .env config file.")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    config = load_config(Path(args.config))
    run_configured_bot(config)


if __name__ == "__main__":
    main()
