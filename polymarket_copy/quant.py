from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .bot import (
    BookCache,
    HttpJsonClient,
    MarketWsBookCache,
    PolymarketCopyBot,
    apply_max_order_usdc,
    best_book_price,
    decimal_value,
)
from .config import CopyBotConfig, validate_config


GAMMA_API = "https://gamma-api.polymarket.com"
OKX_CANDLES_URL = "https://www.okx.com/api/v5/market/history-candles"
MAX_LOCK_TRADES_PER_SIDE = Decimal("20")
SAME_OUTCOME_REPEAT_SEC = 6
SAME_OUTCOME_MIN_PRICE_MOVE = Decimal("0.01")


@dataclass(frozen=True)
class BtcSnapshot:
    source: str
    current_ts: int
    current: Decimal
    start_price_ts: int
    start_price: Decimal
    ret_from_start: Decimal
    ret_1m: Decimal
    ret_3m: Decimal
    up_probability: Decimal


@dataclass(frozen=True)
class QuantMarket:
    slug: str
    title: str
    start_ts: int
    end_ts: int
    outcomes: List[str]
    token_ids: List[str]
    neg_risk: bool

    @property
    def seconds_left(self) -> int:
        return max(0, self.end_ts - int(time.time()))


@dataclass(frozen=True)
class QuantDecision:
    market: QuantMarket
    outcome: str
    token_id: str
    probability: Decimal
    best_ask: Decimal
    raw_edge: Decimal
    edge: Decimal
    limit_price: Decimal
    size: Decimal
    reason: str


class QuantSignalRecorder:
    def __init__(self, config: CopyBotConfig) -> None:
        self.enabled = config.quant_record_signals
        self.path = config.quant_signal_file
        self.interval_sec = config.quant_signal_interval_sec
        self.last_record_ts: Dict[str, float] = {}

    def write(self, record: Dict[str, Any], force: bool = False) -> None:
        if not self.enabled:
            return
        market = record.get("market")
        slug = market.get("slug") if isinstance(market, dict) else ""
        action = str(record.get("action", "unknown"))
        reason = str(record.get("reason", ""))
        key = f"{slug}:{action}:{reason}"
        now = time.time()
        if not force and now - self.last_record_ts.get(key, 0) < self.interval_sec:
            return
        self.last_record_ts[key] = now
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


class ChainlinkRtdsPriceFeed:
    def __init__(self, config: CopyBotConfig) -> None:
        self.config = config
        self.prices: List[Tuple[int, Decimal]] = []
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.ws: Any = None
        self.last_error = ""
        self.last_refresh_request_ts = 0.0

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="chainlink-rtds", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        ws = self.ws
        if ws:
            try:
                ws.close()
            except Exception:
                pass
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)

    def wait_snapshot(self, market_start_ts: int) -> BtcSnapshot:
        self.start()
        deadline = time.monotonic() + self.config.quant_chainlink_timeout_sec
        last_problem = ""
        while time.monotonic() < deadline:
            try:
                return self.snapshot(market_start_ts)
            except RuntimeError as exc:
                last_problem = str(exc)
                if "stale" in last_problem or "no Chainlink prices" in last_problem:
                    self.request_refresh()
                time.sleep(0.25)
        if self.last_error:
            last_problem = f"{last_problem}; last_ws_error={self.last_error}" if last_problem else self.last_error
        raise RuntimeError(f"Chainlink RTDS price unavailable: {last_problem}")

    def snapshot(self, market_start_ts: int) -> BtcSnapshot:
        now = int(time.time())
        with self.lock:
            prices = list(self.prices)
        if not prices:
            raise RuntimeError("no Chainlink prices received yet")

        current_ts, current = prices[-1]
        if now - current_ts > self.config.quant_chainlink_max_age_sec:
            raise RuntimeError(f"latest Chainlink price is stale: age={now - current_ts}s")

        start_item = nearest_price(prices, market_start_ts)
        if start_item is None:
            raise RuntimeError("missing Chainlink start price")
        start_ts, start_price = start_item
        start_delta = abs(start_ts - market_start_ts)
        start_tolerance = self.config.quant_chainlink_start_tolerance_sec
        if start_delta > start_tolerance:
            raise RuntimeError(
                f"Chainlink start price is too far from market start: "
                f"start_ts={start_ts} market_start_ts={market_start_ts} "
                f"delta={start_delta}s tolerance={start_tolerance}s"
            )

        one_min_item = nearest_price(prices, current_ts - 60)
        three_min_item = nearest_price(prices, current_ts - 180)
        one_min_price = one_min_item[1] if one_min_item else start_price
        three_min_price = three_min_item[1] if three_min_item else start_price
        ret_from_start = decimal_return(current, start_price)
        ret_1m = decimal_return(current, one_min_price)
        ret_3m = decimal_return(current, three_min_price)
        up_probability = estimate_up_probability(ret_from_start, ret_1m, ret_3m)
        return BtcSnapshot(
            source="polymarket_rtds_chainlink",
            current_ts=current_ts,
            current=current,
            start_price_ts=start_ts,
            start_price=start_price,
            ret_from_start=ret_from_start,
            ret_1m=ret_1m,
            ret_3m=ret_3m,
            up_probability=up_probability,
        )

    def _run(self) -> None:
        try:
            import websocket  # type: ignore
        except ImportError:
            self.last_error = "missing websocket-client"
            print("[CHAINLINK WARN] 缺少 websocket-client，Chainlink RTDS 不可用。")
            return

        while not self.stop_event.is_set():
            ws = None
            try:
                ws = websocket.create_connection(self.config.quant_chainlink_ws_url, timeout=10)
                self.ws = ws
                self._subscribe(ws)
                print(f"[CHAINLINK] 已连接 Polymarket RTDS，订阅 {self.config.quant_chainlink_symbol}。")
                ws.settimeout(1)
                next_ping = time.monotonic() + 5
                while not self.stop_event.is_set():
                    if time.monotonic() >= next_ping:
                        ws.send("PING")
                        next_ping = time.monotonic() + 5
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
                self.last_error = repr(exc)
                if not self.stop_event.is_set():
                    print(f"[CHAINLINK WARN] RTDS 断开: {repr(exc)}，稍后重连。")
                    time.sleep(max(1.0, self.config.market_ws_reconnect_sec))
            finally:
                self.ws = None
                if ws:
                    try:
                        ws.close()
                    except Exception:
                        pass

    def request_refresh(self) -> None:
        now = time.monotonic()
        if now - self.last_refresh_request_ts < 3:
            return
        self.last_refresh_request_ts = now
        ws = self.ws
        if ws:
            try:
                ws.close()
            except Exception:
                pass

    def _subscribe(self, ws: Any) -> None:
        subscription = {
            "action": "subscribe",
            "subscriptions": [
                {
                    "topic": "crypto_prices_chainlink",
                    "type": "*",
                    "filters": json.dumps({"symbol": self.config.quant_chainlink_symbol}),
                }
            ],
        }
        ws.send(json.dumps(subscription))

    def _handle_message(self, message: str) -> None:
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return
        payload = data.get("payload") if isinstance(data, dict) else None
        if not isinstance(payload, dict):
            return
        rows = payload.get("data")
        if rows is None and ("timestamp" in payload and ("value" in payload or "price" in payload)):
            rows = [payload]
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            return
        parsed: List[Tuple[int, Decimal]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            timestamp = row.get("timestamp") or row.get("ts")
            value = row.get("value") or row.get("price")
            if timestamp is None or value is None:
                continue
            try:
                raw_ts = Decimal(str(timestamp))
                ts = int(raw_ts / Decimal("1000")) if raw_ts > Decimal("10000000000") else int(raw_ts)
                price = decimal_value(value)
            except Exception:
                continue
            parsed.append((ts, price))
        if parsed:
            self._add_prices(parsed)

    def _add_prices(self, parsed: List[Tuple[int, Decimal]]) -> None:
        with self.lock:
            merged = {ts: price for ts, price in self.prices}
            merged.update(parsed)
            cutoff = int(time.time()) - 900
            self.prices = sorted((ts, price) for ts, price in merged.items() if ts >= cutoff)


class QuantStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.state: Dict[str, Any] = {"markets": {}, "lock_markets": {}, "last_order_ts": 0}

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self.state.update(loaded)
        except json.JSONDecodeError:
            backup = self.path.with_suffix(self.path.suffix + ".bad")
            self.path.replace(backup)
            print(f"[WARN] quant state JSON 无效，已备份到 {backup}")

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.state, indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )

    def has_market(self, slug: str) -> bool:
        markets = self.state.get("markets")
        return isinstance(markets, dict) and slug in markets

    def mark_market(self, decision: QuantDecision, mode: str) -> None:
        markets = self.state.setdefault("markets", {})
        markets[decision.market.slug] = {
            "mode": mode,
            "outcome": decision.outcome,
            "raw_edge": str(decision.raw_edge),
            "edge": str(decision.edge),
            "limit_price": str(decision.limit_price),
            "size": str(decision.size),
            "ts": int(time.time()),
        }
        self.state["last_order_ts"] = int(time.time())
        self.save()

    def cooldown_ready(self, cooldown_sec: float) -> bool:
        last_ts = float(self.state.get("last_order_ts") or 0)
        return time.time() - last_ts >= cooldown_sec

    def lock_market_entries(self) -> Dict[str, Dict[str, Any]]:
        markets = self.state.setdefault("lock_markets", {})
        if not isinstance(markets, dict):
            self.state["lock_markets"] = {}
            return {}
        return {str(slug): entry for slug, entry in markets.items() if isinstance(entry, dict)}

    def lock_market_entry(self, market: QuantMarket) -> Dict[str, Any]:
        markets = self.state.setdefault("lock_markets", {})
        entry = markets.setdefault(
            market.slug,
            {
                "title": market.title,
                "start_ts": market.start_ts,
                "end_ts": market.end_ts,
                "locked": False,
                "trades": [],
            },
        )
        if not isinstance(entry, dict):
            entry = {
                "title": market.title,
                "start_ts": market.start_ts,
                "end_ts": market.end_ts,
                "locked": False,
                "trades": [],
            }
            markets[market.slug] = entry
        entry.setdefault("title", market.title)
        entry.setdefault("start_ts", market.start_ts)
        entry.setdefault("end_ts", market.end_ts)
        entry.setdefault("locked", False)
        if not isinstance(entry.get("trades"), list):
            entry["trades"] = []
        return entry

    def append_lock_trade(
        self,
        market: QuantMarket,
        decision: QuantDecision,
        mode: str,
        position_after: Dict[str, Decimal],
        locked_after: bool,
        reason: str,
        status: str = "posted",
        order_response: Optional[Any] = None,
    ) -> None:
        entry = self.lock_market_entry(market)
        now = int(time.time())
        cost = decision.size * decision.limit_price
        entry["trades"].append(
            {
                "ts": now,
                "mode": mode,
                "outcome": decision.outcome,
                "token_id": decision.token_id,
                "probability": str(decision.probability),
                "best_ask": str(decision.best_ask),
                "raw_edge": str(decision.raw_edge),
                "edge": str(decision.edge),
                "limit_price": str(decision.limit_price),
                "size": str(decision.size),
                "cost": str(cost.quantize(Decimal("0.0001"))),
                "reason": reason,
                "status": status,
                "seconds_left": market.seconds_left,
            }
        )
        if order_response is not None:
            entry["trades"][-1]["order_response"] = json_safe(order_response)
        entry["locked"] = bool(locked_after)
        entry["last_trade_ts"] = now
        entry["position"] = lock_position_payload(position_after)
        self.state["last_order_ts"] = now
        self.save()

    def append_lock_order_error(
        self,
        market: QuantMarket,
        decision: QuantDecision,
        reason: str,
        error: str,
        leg_index: int,
    ) -> None:
        entry = self.lock_market_entry(market)
        errors = entry.setdefault("order_errors", [])
        if not isinstance(errors, list):
            errors = []
            entry["order_errors"] = errors
        errors.append(
            {
                "ts": int(time.time()),
                "reason": reason,
                "error": error,
                "leg_index": leg_index,
                "outcome": decision.outcome,
                "token_id": decision.token_id,
                "limit_price": str(decision.limit_price),
                "size": str(decision.size),
                "seconds_left": market.seconds_left,
            }
        )
        self.save()

    def settle_lock_market(self, slug: str, winning_outcome: str, realized_pnl: Decimal) -> None:
        markets = self.state.setdefault("lock_markets", {})
        entry = markets.get(slug) if isinstance(markets, dict) else None
        if not isinstance(entry, dict):
            return
        entry["settled"] = True
        entry["winning_outcome"] = winning_outcome
        entry["realized_pnl"] = str(realized_pnl.quantize(Decimal("0.0001")))
        entry["settled_ts"] = int(time.time())


class PolymarketQuantBot:
    def __init__(self, config: CopyBotConfig) -> None:
        self.config = config
        self.http = HttpJsonClient(config)
        self.market_ws = MarketWsBookCache(config)
        self.book_cache = BookCache(self.http, config, self.market_ws)
        self.order_helper = PolymarketCopyBot(config)
        self.state = QuantStateStore(config.quant_state_file)
        self.signal_recorder = QuantSignalRecorder(config)
        self.chainlink_feed = ChainlinkRtdsPriceFeed(config)
        self.last_log_ts = 0.0

    def run_forever(self) -> None:
        errors, warnings = validate_config(self.config, require_private_key=not self.config.dry_run)
        for warning in warnings:
            print(f"[CONFIG WARN] {warning}")
        if errors:
            raise RuntimeError("; ".join(errors))

        self.state.load()
        client = None
        if self.config.dry_run:
            print("[MODE] AI量化 DRY_RUN=1，只打印，不真实下单。")
        else:
            client = self.order_helper.build_client()
            print("[MODE] AI量化 DRY_RUN=0，真实下单。")
        print(
            "[AI QUANT] "
            f"symbol={self.config.quant_symbol} "
            f"strategy={self.config.quant_strategy} "
            f"source={self.config.quant_price_source} "
            f"size_mode={self.config.quant_size_mode} "
            f"order_usdc={self.config.quant_order_usdc} "
            f"order_shares={self.config.quant_order_shares} "
            f"capital={self.config.quant_capital_usdc} "
            f"market_cap={self.config.quant_market_max_usdc} "
            f"max_drawdown={self.config.quant_max_drawdown_usdc} "
            f"min_edge={self.config.quant_min_edge} "
            f"arb_min_profit={self.config.quant_arbitrage_min_profit} "
            f"entry_window={self.config.quant_min_seconds_left}-{self.config.quant_max_seconds_left}s"
        )
        if self.config.quant_record_signals:
            print(f"[AI QUANT DATA] signals={self.config.quant_signal_file}")
        if self.config.quant_price_source == "chainlink":
            self.chainlink_feed.start()

        try:
            while True:
                try:
                    self.run_once(client=client)
                    time.sleep(self.config.poll_sec)
                except KeyboardInterrupt:
                    print("退出。")
                    break
                except Exception as exc:
                    print(f"[AI QUANT ERROR] {repr(exc)}")
                    time.sleep(max(self.config.poll_sec, 3))
        finally:
            self.market_ws.stop()
            self.chainlink_feed.stop()

    def run_once(self, client: Any = None) -> Optional[QuantDecision]:
        market = self.find_current_market()
        if not market:
            self.log_throttled("[AI QUANT] 未找到当前 BTC 5m 市场，等待下一轮。")
            return None

        self.market_ws.start(market.token_ids)
        try:
            snapshot = self.fetch_btc_snapshot(market.start_ts)
        except RuntimeError as exc:
            self.log_throttled(f"[AI QUANT] Chainlink 价格暂不可用，跳过本轮: {exc}")
            return None
        if self.config.quant_strategy == "lock":
            return self.make_lock_decision(market, snapshot, client=client)

        decision = self.make_decision(market, snapshot)
        if decision is None:
            return None

        mode = "DRY_RUN" if self.config.dry_run else "LIVE"
        print(
            "[AI QUANT BUY] "
            f"mode={mode} market={decision.market.title} outcome={decision.outcome} "
            f"prob={decision.probability} best_ask={decision.best_ask} "
            f"edge={decision.edge} price={decision.best_ask} size={decision.size} "
            f"ret_start={snapshot.ret_from_start} ret_1m={snapshot.ret_1m} "
            f"seconds_left={market.seconds_left} reason={decision.reason}"
        )
        if self.config.dry_run:
            self.state.mark_market(decision, mode="dry_run")
            return decision

        reviewed, review = self.second_review_buy(decision)
        if reviewed is None:
            print(f"[AI QUANT SKIP] second_review_failed market={market.title} review={review}")
            self.record_signal(
                market,
                snapshot,
                "skip",
                "second_review_failed",
                [review],
                selected=decision,
                force=True,
            )
            return None

        book = self.fetch_fresh_book(reviewed.token_id)
        tick_size = decimal_value(book.get("tick_size", "0.01"))
        neg_risk = bool(book.get("neg_risk", market.neg_risk))
        self.order_helper._post_order(  # noqa: SLF001 - shared local order helper
            client,
            reviewed.token_id,
            reviewed.limit_price,
            reviewed.size,
            "BUY",
            tick_size,
            neg_risk,
        )
        self.state.mark_market(reviewed, mode="live")
        return reviewed

    def find_current_market(self) -> Optional[QuantMarket]:
        now = int(time.time())
        start = (now // 300) * 300
        for candidate_start in (start, start - 300, start + 300):
            market = self.fetch_market_by_start(candidate_start)
            if not market:
                continue
            if market.start_ts <= now < market.end_ts:
                return market
        return None

    def fetch_market_by_start(self, start_ts: int) -> Optional[QuantMarket]:
        slug = f"{self.config.quant_market_slug_prefix}-{start_ts}"
        try:
            data = self.http.get_json(f"{GAMMA_API}/events/slug/{slug}")
        except Exception:
            return None
        markets = data.get("markets") if isinstance(data, dict) else None
        if not isinstance(markets, list) or not markets:
            return None
        market = markets[0]
        if not market.get("acceptingOrders", False):
            return None
        outcomes = parse_json_list(market.get("outcomes"))
        token_ids = parse_json_list(market.get("clobTokenIds"))
        if len(outcomes) != len(token_ids) or "Up" not in outcomes or "Down" not in outcomes:
            return None
        event_start = market.get("eventStartTime") or data.get("startTime")
        end_date = market.get("endDate") or data.get("endDate")
        if not event_start or not end_date:
            return None
        return QuantMarket(
            slug=slug,
            title=str(market.get("question") or data.get("title") or slug),
            start_ts=iso_to_ts(str(event_start)),
            end_ts=iso_to_ts(str(end_date)),
            outcomes=[str(outcome) for outcome in outcomes],
            token_ids=[str(token_id) for token_id in token_ids],
            neg_risk=bool(market.get("negRisk", False)),
        )

    def fetch_btc_snapshot(self, market_start_ts: int) -> BtcSnapshot:
        if self.config.quant_price_source == "chainlink":
            return self.chainlink_feed.wait_snapshot(market_start_ts)
        if self.config.quant_price_source != "okx":
            raise RuntimeError("AI量化行情源只支持 QUANT_PRICE_SOURCE=chainlink 或 okx")

        data = self.http.get_json(
            OKX_CANDLES_URL,
            params={"instId": self.config.quant_symbol, "bar": "1m", "limit": 10},
        )
        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list) or len(rows) < 4:
            raise RuntimeError(f"OKX candles 返回异常: {data}")

        candles = sorted(parse_okx_candles(rows), key=lambda item: item["ts"])
        current = candles[-1]["close"]
        start_price = nearest_candle_open(candles, market_start_ts)
        ret_from_start = decimal_return(current, start_price)
        ret_1m = decimal_return(current, candles[-2]["close"])
        ret_3m = decimal_return(current, candles[-4]["close"])
        up_probability = estimate_up_probability(ret_from_start, ret_1m, ret_3m)
        return BtcSnapshot(
            source="okx",
            current_ts=int(candles[-1]["ts"]),
            current=current,
            start_price_ts=int(nearest_candle_ts(candles, market_start_ts)),
            start_price=start_price,
            ret_from_start=ret_from_start,
            ret_1m=ret_1m,
            ret_3m=ret_3m,
            up_probability=up_probability,
        )

    def fetch_fresh_book(self, token_id: str) -> Dict[str, Any]:
        book = self.http.get_json(
            f"{self.config.clob_api_url}/book",
            params={"token_id": token_id},
        )
        if self.market_ws:
            self.market_ws.remember_book(token_id, book)
        return book

    def second_review_buy(self, decision: QuantDecision) -> Tuple[Optional[QuantDecision], Dict[str, Any]]:
        review: Dict[str, Any] = {
            "outcome": decision.outcome,
            "token_id": decision.token_id,
            "original_price": decision.limit_price,
            "size": decision.size,
        }
        try:
            book = self.fetch_fresh_book(decision.token_id)
            best_ask = best_book_price(book, "BUY")
            if best_ask is None:
                review["valid"] = False
                review["skip_reason"] = "no_best_ask"
                return None, review
            tick_size = decimal_value(book.get("tick_size", "0.01"))
            min_order_size = decimal_value(book.get("min_order_size", "1"))
            review.update(
                {
                    "best_ask": best_ask,
                    "refreshed_price": best_ask,
                    "tick_size": tick_size,
                    "min_order_size": min_order_size,
                }
            )
            if decision.size < min_order_size:
                review["valid"] = False
                review["skip_reason"] = "size_below_min_order"
                return None, review
            reviewed = QuantDecision(
                market=decision.market,
                outcome=decision.outcome,
                token_id=decision.token_id,
                probability=decision.probability,
                best_ask=best_ask,
                raw_edge=(decision.probability - best_ask).quantize(Decimal("0.0001")),
                edge=(decision.probability - best_ask).quantize(Decimal("0.0001")),
                limit_price=best_ask,
                size=decision.size,
                reason=f"{decision.reason} | second_review",
            )
            review["valid"] = True
            return reviewed, review
        except Exception as exc:
            review["valid"] = False
            review["skip_reason"] = f"review_error:{type(exc).__name__}"
            review["error"] = repr(exc)
            return None, review

    def make_decision(self, market: QuantMarket, snapshot: BtcSnapshot) -> Optional[QuantDecision]:
        dry_run_rejections: List[str] = []
        if not self.state.cooldown_ready(self.config.quant_cooldown_sec):
            self.log_throttled("[AI QUANT] 冷却中，暂不下单。")
            if self.config.dry_run:
                dry_run_rejections.append("cooldown")
            else:
                self.record_signal(market, snapshot, "skip", "cooldown", [], force=True)
                return None
        if market.seconds_left < self.config.quant_min_seconds_left:
            self.log_throttled(
                f"[AI QUANT] {market.title} 剩余 {market.seconds_left}s，低于最小剩余时间，跳过。"
            )
            if self.config.dry_run:
                dry_run_rejections.append("low_seconds_left")
            else:
                self.record_signal(market, snapshot, "skip", "low_seconds_left", [], force=True)
                return None
        if market.seconds_left > self.config.quant_max_seconds_left:
            self.log_throttled(
                f"[AI QUANT] {market.title} remaining={market.seconds_left}s above entry window, wait."
            )
            if self.config.dry_run:
                dry_run_rejections.append("too_early")
            else:
                self.record_signal(market, snapshot, "skip", "too_early", [], force=True)
                return None

        candidates: List[QuantDecision] = []
        candidate_records: List[Dict[str, Any]] = []
        for outcome, token_id in zip(market.outcomes, market.token_ids):
            probability = snapshot.up_probability if outcome == "Up" else Decimal("1") - snapshot.up_probability
            try:
                book = self.book_cache.get_book(token_id)
            except Exception as exc:
                candidate_records.append(
                    {
                        "outcome": outcome,
                        "token_id": token_id,
                        "probability": probability.quantize(Decimal("0.0001")),
                        "valid": False,
                        "skip_reason": f"book_error:{type(exc).__name__}",
                    }
                )
                continue
            best_ask = best_book_price(book, "BUY")
            if best_ask is None:
                candidate_records.append(
                    {
                        "outcome": outcome,
                        "token_id": token_id,
                        "probability": probability.quantize(Decimal("0.0001")),
                        "valid": False,
                        "skip_reason": "no_best_ask",
                    }
                )
                continue
            limit_price = best_ask
            raw_edge = probability - best_ask
            edge = raw_edge
            size = (self.config.quant_order_usdc / best_ask).quantize(
                Decimal("0.000001"),
                rounding=ROUND_FLOOR,
            )
            size = apply_max_order_usdc(size, best_ask, self.config.max_order_usdc)
            min_order_size = decimal_value(book.get("min_order_size", "1"))
            if size < min_order_size:
                self.log_throttled(
                    f"[AI QUANT] {outcome} size={size} 小于最小下单 {min_order_size}，跳过。"
                )
                candidate_records.append(
                    {
                        "outcome": outcome,
                        "token_id": token_id,
                        "probability": probability.quantize(Decimal("0.0001")),
                        "best_ask": best_ask,
                        "raw_edge": raw_edge.quantize(Decimal("0.0001")),
                        "edge": edge.quantize(Decimal("0.0001")),
                        "limit_price": limit_price,
                        "size": size,
                        "min_order_size": min_order_size,
                        "valid": False,
                        "skip_reason": "size_below_min_order",
                    }
                )
                continue
            decision = QuantDecision(
                market=market,
                outcome=outcome,
                token_id=token_id,
                probability=probability.quantize(Decimal("0.0001")),
                best_ask=best_ask,
                raw_edge=raw_edge.quantize(Decimal("0.0001")),
                edge=edge.quantize(Decimal("0.0001")),
                limit_price=limit_price,
                size=size,
                reason=(
                    f"p({outcome})={probability.quantize(Decimal('0.0001'))} "
                    f"> ask={best_ask} + edge={edge.quantize(Decimal('0.0001'))}"
                ),
            )
            candidates.append(decision)
            candidate_records.append(self.decision_payload(decision) | {"valid": True})

        if not candidates:
            self.log_throttled("[AI QUANT] 没有可用盘口候选。")
            self.record_signal(
                market,
                snapshot,
                "would_skip" if self.config.dry_run else "skip",
                "no_valid_candidates",
                candidate_records,
                force=self.config.dry_run,
            )
            return None

        best = max(candidates, key=lambda item: item.edge)
        if dry_run_rejections:
            self.record_signal(
                market,
                snapshot,
                "would_skip",
                ",".join(dry_run_rejections),
                candidate_records,
                selected=best,
                force=True,
            )
        if best.edge < self.config.quant_min_edge:
            self.log_throttled(
                "[AI QUANT] "
                f"{market.title} p_up={snapshot.up_probability} "
                f"best={best.outcome} edge={best.edge} < min_edge={self.config.quant_min_edge}，跳过。"
            )
            self.record_signal(
                market,
                snapshot,
                "would_skip" if self.config.dry_run else "skip",
                "edge_below_min",
                candidate_records,
                selected=best,
                force=self.config.dry_run,
            )
            if not self.config.dry_run:
                return None
        self.record_signal(
            market,
            snapshot,
            "would_buy" if self.config.dry_run else "live_buy",
            "edge_passed",
            candidate_records,
            selected=best,
            force=True,
        )
        return best

    def settle_finished_lock_markets(self) -> None:
        now = int(time.time())
        changed = False
        for slug, entry in self.state.lock_market_entries().items():
            if entry.get("settled"):
                continue
            trades = entry.get("trades")
            if not isinstance(trades, list) or not trades:
                continue
            end_ts = int(state_decimal(entry.get("end_ts")))
            if end_ts <= 0 or now < end_ts + 15:
                continue
            last_check = int(state_decimal(entry.get("last_settlement_check_ts")))
            if last_check and now - last_check < 20:
                continue
            entry["last_settlement_check_ts"] = now
            changed = True
            winning_outcome = self.fetch_market_winning_outcome(slug)
            if not winning_outcome:
                continue
            position = lock_position_from_entry(entry)
            realized_pnl = position["up_pnl"] if winning_outcome == "Up" else position["down_pnl"]
            self.state.settle_lock_market(slug, winning_outcome, realized_pnl)
            print(
                "[AI LOCK SETTLE] "
                f"market={slug} winner={winning_outcome} realized_pnl={realized_pnl} "
                f"total_cost={position['total_cost']}"
            )
        if changed:
            self.state.save()

    def fetch_market_winning_outcome(self, slug: str) -> Optional[str]:
        try:
            data = self.http.get_json(f"{GAMMA_API}/events/slug/{slug}")
        except Exception as exc:
            self.log_throttled(f"[AI LOCK] 结算查询失败: {slug} {type(exc).__name__}")
            return None
        markets = data.get("markets") if isinstance(data, dict) else None
        if not isinstance(markets, list) or not markets:
            return None
        market = markets[0]
        if not market.get("closed"):
            return None
        outcomes = parse_json_list(market.get("outcomes"))
        prices = parse_json_list(market.get("outcomePrices"))
        if len(outcomes) != len(prices):
            return None
        for outcome, price in zip(outcomes, prices):
            try:
                if decimal_value(price) >= Decimal("0.99"):
                    return str(outcome)
            except Exception:
                continue
        return None

    def lock_bankroll_snapshot(
        self,
        current_slug: str,
        current_cost: Decimal,
        current_worst_pnl: Decimal = Decimal("0"),
    ) -> Dict[str, Decimal]:
        realized_pnl = Decimal("0")
        other_unsettled_cost = Decimal("0")
        other_unsettled_worst_pnl = Decimal("0")
        unsettled_count = 0
        settled_count = 0
        for slug, entry in self.state.lock_market_entries().items():
            if entry.get("settled"):
                realized_pnl += state_decimal(entry.get("realized_pnl"))
                settled_count += 1
                continue
            if slug == current_slug:
                continue
            position = lock_position_from_entry(entry)
            if position["total_cost"] > 0:
                other_unsettled_cost += position["total_cost"]
                other_unsettled_worst_pnl += position["worst_pnl"]
                unsettled_count += 1
        equity = self.config.quant_capital_usdc + realized_pnl
        open_worst_pnl = current_worst_pnl + other_unsettled_worst_pnl
        risk_equity = equity + open_worst_pnl
        risk_drawdown = self.config.quant_capital_usdc - risk_equity
        available_to_add = equity - other_unsettled_cost - current_cost
        return {
            "capital_usdc": self.config.quant_capital_usdc,
            "realized_pnl": realized_pnl,
            "equity": equity,
            "other_unsettled_cost": other_unsettled_cost,
            "other_unsettled_worst_pnl": other_unsettled_worst_pnl,
            "current_market_cost": current_cost,
            "current_market_worst_pnl": current_worst_pnl,
            "open_worst_pnl": open_worst_pnl,
            "risk_equity": risk_equity,
            "risk_drawdown_usdc": max(risk_drawdown, Decimal("0")),
            "available_to_add": max(available_to_add, Decimal("0")),
            "raw_available_to_add": available_to_add,
            "unsettled_markets": Decimal(unsettled_count),
            "settled_markets": Decimal(settled_count),
        }

    def lock_loop_snapshot(self, market: QuantMarket, position: Dict[str, Decimal]) -> Dict[str, Any]:
        asks: Dict[str, Any] = {
            "up_ask": None,
            "down_ask": None,
            "sum_ask": None,
            "worst_pnl": position["worst_pnl"],
            "up_pnl": position["up_pnl"],
            "down_pnl": position["down_pnl"],
        }
        for outcome, token_id in zip(market.outcomes, market.token_ids):
            key = "up_ask" if outcome == "Up" else "down_ask" if outcome == "Down" else ""
            if not key:
                continue
            try:
                book = self.book_cache.get_book(token_id)
                best_ask = best_book_price(book, "BUY")
                asks[key] = best_ask
            except Exception as exc:
                asks[f"{key}_error"] = f"{type(exc).__name__}:{exc}"
        up_ask = asks.get("up_ask")
        down_ask = asks.get("down_ask")
        if isinstance(up_ask, Decimal) and isinstance(down_ask, Decimal):
            asks["sum_ask"] = up_ask + down_ask
            asks["pure_arb_edge"] = Decimal("1") - asks["sum_ask"]
        return asks

    def make_lock_decision(
        self,
        market: QuantMarket,
        snapshot: BtcSnapshot,
        client: Any = None,
    ) -> Optional[QuantDecision]:
        self.settle_finished_lock_markets()
        entry = self.state.lock_market_entry(market)
        position_before = lock_position_from_entry(entry)
        bankroll_before = self.lock_bankroll_snapshot(
            market.slug,
            position_before["total_cost"],
            position_before["worst_pnl"],
        )
        def block_or_record(reason: str, message: str = "") -> bool:
            if message:
                self.log_throttled(message)
            self.record_lock_signal(
                market,
                snapshot,
                action="would_skip" if self.config.dry_run else "skip",
                reason=reason,
                candidates=[],
                position_before=position_before,
                position_after=position_before,
                bankroll_before=bankroll_before,
                bankroll_after=bankroll_before,
                force=True,
            )
            return True

        if bankroll_before["equity"] <= 0 and block_or_record(
            "equity_depleted",
            f"[AI LOCK] 模拟本金已耗尽 equity={bankroll_before['equity']}，停止新增模拟单。",
        ):
            return None
        if (
            self.config.quant_max_drawdown_usdc > 0
            and bankroll_before["risk_drawdown_usdc"] >= self.config.quant_max_drawdown_usdc
            and block_or_record(
                "max_drawdown_reached",
                "[AI LOCK] "
                f"已达到最大模拟回撤 realized_pnl={bankroll_before['realized_pnl']} "
                f"limit=-{self.config.quant_max_drawdown_usdc}，停止新增模拟单。",
            )
        ):
            return None
        if self.config.quant_lock_stop_on_lock and bool(entry.get("locked")) and block_or_record(
            "market_locked",
            f"[AI LOCK] {market.slug} 已经锁利，停止本市场。",
        ):
            return None
        if market.seconds_left < self.config.quant_min_seconds_left and block_or_record(
            "low_seconds_left",
            f"[AI LOCK] {market.title} 剩余 {market.seconds_left}s，低于最小剩余时间，跳过。",
        ):
            return None
        if market.seconds_left > self.config.quant_max_seconds_left and block_or_record(
            "too_early",
            f"[AI LOCK] {market.title} remaining={market.seconds_left}s above entry window, wait.",
        ):
            return None
        if position_before["trade_count"] >= Decimal(str(self.config.quant_max_trades_per_market)) and block_or_record(
            "max_trades_reached",
            f"[AI LOCK] {market.slug} 已达到单市场最多笔数，跳过。",
        ):
            return None
        last_trade_ts = float(entry.get("last_trade_ts") or 0)
        if (
            last_trade_ts
            and time.time() - last_trade_ts < self.config.quant_rebuy_cooldown_sec
            and block_or_record("rebuy_cooldown")
        ):
            return None

        candidates, candidate_records = self.build_lock_candidates(
            market,
            snapshot,
            position_before,
            bankroll_before,
            entry,
        )
        selected = self.select_lock_candidate(candidates)
        if selected is None:
            self.log_throttled(
                "[AI LOCK] "
                f"{market.title} 暂无锁利/补单候选，cost={position_before['total_cost']} "
                f"up_pnl={position_before['up_pnl']} down_pnl={position_before['down_pnl']} "
                f"available={bankroll_before['available_to_add']}。"
            )
            self.record_lock_signal(
                market,
                snapshot,
                action="would_skip" if self.config.dry_run else "skip",
                reason="no_lock_candidate",
                candidates=candidate_records,
                position_before=position_before,
                position_after=position_before,
                bankroll_before=bankroll_before,
                bankroll_after=bankroll_before,
                force=self.config.dry_run,
            )
            return None
        all_rejections = list(selected.get("dry_run_rejections") or [])
        selected_score = selected.get("score")
        if self.config.dry_run and isinstance(selected_score, tuple) and selected_score[0] <= 0:
            weak_reason = str(selected.get("reason") or "weak_candidate")
            if weak_reason not in all_rejections:
                all_rejections.append(weak_reason)
        if all_rejections:
            selected = dict(selected)
            selected["dry_run_rejections"] = all_rejections

        return self.execute_lock_plan(
            market,
            snapshot,
            selected,
            candidate_records,
            position_before,
            bankroll_before,
            client=client,
        )

    def execute_lock_plan(
        self,
        market: QuantMarket,
        snapshot: BtcSnapshot,
        selected: Dict[str, Any],
        candidate_records: List[Dict[str, Any]],
        position_before: Dict[str, Decimal],
        bankroll_before: Dict[str, Decimal],
        client: Any = None,
    ) -> Optional[QuantDecision]:
        if self.config.dry_run:
            return self.execute_lock_paper(
                market,
                snapshot,
                selected,
                candidate_records,
                position_before,
                bankroll_before,
            )
        return self.execute_lock_live(
            market,
            snapshot,
            selected,
            candidate_records,
            position_before,
            bankroll_before,
            client=client,
        )

    def execute_lock_paper(
        self,
        market: QuantMarket,
        snapshot: BtcSnapshot,
        selected: Dict[str, Any],
        candidate_records: List[Dict[str, Any]],
        position_before: Dict[str, Decimal],
        bankroll_before: Dict[str, Decimal],
    ) -> Optional[QuantDecision]:
        decision = selected["decision"]
        legs = self.lock_plan_legs(selected)
        paper_legs = self.paper_lock_plan_legs(selected)
        position_after = selected["position_after"]
        bankroll_after = self.lock_bankroll_snapshot(
            market.slug,
            position_after["total_cost"],
            position_after["worst_pnl"],
        )
        locked_after = position_after["worst_pnl"] >= self.config.quant_lock_min_profit
        reason = str(selected["reason"])
        print(
            "[AI LOCK BUY] "
            f"mode=DRY_RUN market={decision.market.title} legs={len(legs)} "
            f"outcome={decision.outcome} size={decision.size} price={decision.best_ask} "
            f"cost={selected['notional']} edge={decision.edge} "
            f"up_pnl={position_after['up_pnl']} down_pnl={position_after['down_pnl']} "
            f"total_cost={position_after['total_cost']} trades={position_after['trade_count']} "
            f"up_trades={position_after['up_trade_count']} down_trades={position_after['down_trade_count']} "
            f"available={bankroll_after['available_to_add']} "
            f"locked={1 if locked_after else 0} reason={reason}"
        )
        self.record_lock_signal(
            market,
            snapshot,
            action="lock_would_buy",
            reason=reason,
            candidates=candidate_records,
            selected=decision,
            selected_meta=lock_candidate_payload(selected),
            position_before=position_before,
            position_after=position_after,
            bankroll_before=bankroll_before,
            bankroll_after=bankroll_after,
            force=True,
        )
        running_position = position_before
        for index, leg in enumerate(paper_legs):
            running_position = lock_position_after_trade(
                running_position,
                leg.outcome,
                leg.size,
                leg.limit_price,
            )
            self.state.append_lock_trade(
                market,
                leg,
                mode="dry_run",
                position_after=running_position,
                locked_after=locked_after if index == len(paper_legs) - 1 else False,
                reason=reason,
                status="paper",
            )
        return decision

    def execute_lock_live(
        self,
        market: QuantMarket,
        snapshot: BtcSnapshot,
        selected: Dict[str, Any],
        candidate_records: List[Dict[str, Any]],
        position_before: Dict[str, Decimal],
        bankroll_before: Dict[str, Decimal],
        client: Any = None,
    ) -> Optional[QuantDecision]:
        if client is None:
            raise RuntimeError("lock live executor requires an initialized CLOB client")
        reviewed, review_records = self.review_lock_plan(selected, position_before, bankroll_before)
        if reviewed is None:
            self.record_lock_signal(
                market,
                snapshot,
                action="skip",
                reason="second_review_failed",
                candidates=candidate_records + review_records,
                position_before=position_before,
                position_after=position_before,
                bankroll_before=bankroll_before,
                bankroll_after=bankroll_before,
                selected_meta={"review_records": review_records},
                force=True,
            )
            print(f"[AI LOCK SKIP] second_review_failed market={market.title} review={review_records}")
            return None

        legs = self.lock_plan_legs(reviewed)
        decision = reviewed["decision"]
        reason = str(reviewed["reason"])
        position_after = reviewed["position_after"]
        bankroll_after = self.lock_bankroll_snapshot(
            market.slug,
            position_after["total_cost"],
            position_after["worst_pnl"],
        )
        locked_after = position_after["worst_pnl"] >= self.config.quant_lock_min_profit
        running_position = position_before
        posted_legs = 0
        for index, leg in enumerate(legs):
            try:
                book = self.fetch_fresh_book(leg.token_id)
                tick_size = decimal_value(book.get("tick_size", "0.01"))
                neg_risk = bool(book.get("neg_risk", market.neg_risk))
                response = self.order_helper._post_order(  # noqa: SLF001 - shared local order helper
                    client,
                    leg.token_id,
                    leg.limit_price,
                    leg.size,
                    "BUY",
                    tick_size,
                    neg_risk,
                )
                running_position = lock_position_after_trade(
                    running_position,
                    leg.outcome,
                    leg.size,
                    leg.limit_price,
                )
                self.state.append_lock_trade(
                    market,
                    leg,
                    mode="live",
                    position_after=running_position,
                    locked_after=locked_after if index == len(legs) - 1 else False,
                    reason=reason,
                    status="posted",
                    order_response=response,
                )
                posted_legs += 1
            except Exception as exc:
                fail_reason = "first_leg_failed" if index == 0 else "second_leg_failed"
                self.state.append_lock_order_error(market, leg, fail_reason, repr(exc), index)
                self.record_lock_signal(
                    market,
                    snapshot,
                    action="lock_live_error",
                    reason=fail_reason,
                    candidates=candidate_records + review_records,
                    selected=leg,
                    selected_meta={"posted_legs": posted_legs, "error": repr(exc)},
                    position_before=position_before,
                    position_after=running_position,
                    bankroll_before=bankroll_before,
                    bankroll_after=self.lock_bankroll_snapshot(
                        market.slug,
                        running_position["total_cost"],
                        running_position["worst_pnl"],
                    ),
                    force=True,
                )
                print(
                    "[AI LOCK LIVE ERROR] "
                    f"market={market.title} reason={fail_reason} posted_legs={posted_legs} error={repr(exc)}"
                )
                return legs[0] if posted_legs else None

        print(
            "[AI LOCK BUY] "
            f"mode=LIVE market={market.title} legs={len(legs)} cost={reviewed['notional']} "
            f"up_pnl={position_after['up_pnl']} down_pnl={position_after['down_pnl']} "
            f"locked={1 if locked_after else 0} reason={reason}"
        )
        self.record_lock_signal(
            market,
            snapshot,
            action="lock_live_buy",
            reason=reason,
            candidates=candidate_records + review_records,
            selected=decision,
            selected_meta=lock_candidate_payload(reviewed) | {"review_records": review_records},
            position_before=position_before,
            position_after=position_after,
            bankroll_before=bankroll_before,
            bankroll_after=bankroll_after,
            force=True,
        )
        return decision

    def review_lock_plan(
        self,
        selected: Dict[str, Any],
        position_before: Dict[str, Decimal],
        bankroll_before: Dict[str, Decimal],
    ) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
        reviewed_legs: List[QuantDecision] = []
        review_records: List[Dict[str, Any]] = []
        for leg in self.lock_plan_legs(selected):
            reviewed, review = self.second_review_buy(leg)
            review_records.append(review | {"review_type": "lock_leg"})
            if reviewed is None:
                return None, review_records
            reviewed_legs.append(reviewed)
        if not reviewed_legs:
            return None, review_records
        if str(selected.get("reason")) == "pure_arbitrage" and len(reviewed_legs) == 2:
            sum_ask = reviewed_legs[0].best_ask + reviewed_legs[1].best_ask
            review_records.append(
                {
                    "review_type": "pure_arbitrage",
                    "sum_ask": sum_ask,
                    "min_profit": self.config.quant_arbitrage_min_profit,
                    "valid": sum_ask < Decimal("1") - self.config.quant_arbitrage_min_profit,
                }
            )
            if sum_ask >= Decimal("1") - self.config.quant_arbitrage_min_profit:
                return None, review_records
        position_after = position_before
        notional = Decimal("0")
        for leg in reviewed_legs:
            position_after = lock_position_after_trade(position_after, leg.outcome, leg.size, leg.limit_price)
            notional += leg.size * leg.limit_price
        if position_after["total_cost"] > self.config.quant_market_max_usdc:
            review_records.append({"review_type": "budget", "valid": False, "skip_reason": "market_cap_reached"})
            return None, review_records
        if notional > bankroll_before["available_to_add"]:
            review_records.append({"review_type": "budget", "valid": False, "skip_reason": "bankroll_cap_reached"})
            return None, review_records
        reviewed = dict(selected)
        reviewed["legs"] = reviewed_legs
        reviewed["decision"] = reviewed_legs[0]
        reviewed["position_after"] = position_after
        reviewed["notional"] = notional.quantize(Decimal("0.0001"))
        reviewed["improvement_worst_pnl"] = position_after["worst_pnl"] - position_before["worst_pnl"]
        return reviewed, review_records

    def lock_plan_legs(self, selected: Dict[str, Any]) -> List[QuantDecision]:
        legs = selected.get("legs")
        if isinstance(legs, list) and all(isinstance(leg, QuantDecision) for leg in legs):
            return legs
        decision = selected.get("decision")
        return [decision] if isinstance(decision, QuantDecision) else []

    def paper_lock_plan_legs(self, selected: Dict[str, Any]) -> List[QuantDecision]:
        paper_legs: List[QuantDecision] = []
        for leg in self.lock_plan_legs(selected):
            paper_legs.append(
                QuantDecision(
                    market=leg.market,
                    outcome=leg.outcome,
                    token_id=leg.token_id,
                    probability=leg.probability,
                    best_ask=leg.best_ask,
                    raw_edge=leg.raw_edge,
                    edge=leg.edge,
                    limit_price=leg.best_ask,
                    size=leg.size,
                    reason=f"{leg.reason} | paper_price={leg.best_ask}",
                )
            )
        return paper_legs

    def build_lock_candidates(
        self,
        market: QuantMarket,
        snapshot: BtcSnapshot,
        position_before: Dict[str, Decimal],
        bankroll_before: Dict[str, Decimal],
        entry: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        candidates: List[Dict[str, Any]] = []
        candidate_records: List[Dict[str, Any]] = []
        outcome_items: Dict[str, Dict[str, Any]] = {}
        market_budget_remaining = self.config.quant_market_max_usdc - position_before["total_cost"]
        budget_remaining = min(bankroll_before["available_to_add"], market_budget_remaining)
        for outcome, token_id in zip(market.outcomes, market.token_ids):
            probability = snapshot.up_probability if outcome == "Up" else Decimal("1") - snapshot.up_probability
            try:
                book = self.book_cache.get_book(token_id)
            except Exception as exc:
                candidate_records.append(
                    {
                        "outcome": outcome,
                        "token_id": token_id,
                        "probability": probability.quantize(Decimal("0.0001")),
                        "valid": False,
                        "skip_reason": f"book_error:{type(exc).__name__}",
                    }
                )
                continue
            best_ask = best_book_price(book, "BUY")
            if best_ask is None:
                candidate_records.append(
                    {
                        "outcome": outcome,
                        "token_id": token_id,
                        "probability": probability.quantize(Decimal("0.0001")),
                        "valid": False,
                        "skip_reason": "no_best_ask",
                    }
                )
                continue
            limit_price = best_ask
            raw_edge = probability - best_ask
            edge = raw_edge
            if self.config.quant_size_mode == "shares":
                size = self.config.quant_order_shares.quantize(Decimal("0.000001"), rounding=ROUND_FLOOR)
            else:
                size = (self.config.quant_order_usdc / best_ask).quantize(
                    Decimal("0.000001"),
                    rounding=ROUND_FLOOR,
                )
            size = apply_max_order_usdc(size, best_ask, self.config.max_order_usdc)
            min_order_size = decimal_value(book.get("min_order_size", "1"))
            if size < min_order_size:
                candidate_records.append(
                    {
                        "outcome": outcome,
                        "token_id": token_id,
                        "probability": probability.quantize(Decimal("0.0001")),
                        "best_ask": best_ask,
                        "raw_edge": raw_edge.quantize(Decimal("0.0001")),
                        "edge": edge.quantize(Decimal("0.0001")),
                        "limit_price": limit_price,
                        "size": size,
                        "min_order_size": min_order_size,
                        "valid": False,
                        "skip_reason": "size_below_min_order",
                    }
                )
                continue
            side_count_key = "up_trade_count" if outcome == "Up" else "down_trade_count"
            side_trade_count_before = position_before[side_count_key]
            if side_trade_count_before >= MAX_LOCK_TRADES_PER_SIDE:
                candidate_records.append(
                    {
                        "outcome": outcome,
                        "token_id": token_id,
                        "probability": probability.quantize(Decimal("0.0001")),
                        "best_ask": best_ask,
                        "raw_edge": raw_edge.quantize(Decimal("0.0001")),
                        "edge": edge.quantize(Decimal("0.0001")),
                        "limit_price": limit_price,
                        "size": size,
                        "side_trade_count_before": int(side_trade_count_before),
                        "side_trade_limit": int(MAX_LOCK_TRADES_PER_SIDE),
                        "valid": False,
                        "skip_reason": "side_trade_cap_reached",
                    }
                )
                continue
            notional = (size * best_ask).quantize(Decimal("0.0001"))
            max_notional = notional
            dry_run_rejections: List[str] = []
            repeat_skip_reason = same_outcome_repeat_skip(entry, outcome, best_ask)
            if repeat_skip_reason:
                dry_run_rejections.append(repeat_skip_reason)
            if max_notional > budget_remaining:
                if self.config.dry_run:
                    dry_run_rejections.append("budget_cap_reached")
                else:
                    candidate_records.append(
                        {
                            "outcome": outcome,
                            "token_id": token_id,
                            "probability": probability.quantize(Decimal("0.0001")),
                            "best_ask": best_ask,
                            "raw_edge": raw_edge.quantize(Decimal("0.0001")),
                            "edge": edge.quantize(Decimal("0.0001")),
                            "limit_price": limit_price,
                            "size": size,
                            "notional": notional,
                            "max_notional": max_notional,
                            "budget_remaining": budget_remaining,
                            "market_budget_remaining": market_budget_remaining,
                            "bankroll_before": lock_bankroll_payload(bankroll_before),
                            "position_before": lock_position_payload(position_before),
                            "valid": False,
                            "skip_reason": "budget_cap_reached",
                        }
                    )
                    continue

            position_after = lock_position_after_trade(position_before, outcome, size, best_ask)
            improvement = position_after["worst_pnl"] - position_before["worst_pnl"]
            would_lock = position_after["worst_pnl"] >= self.config.quant_lock_min_profit
            open_worst_after = bankroll_before["other_unsettled_worst_pnl"] + position_after["worst_pnl"]
            risk_equity_after = (
                self.config.quant_capital_usdc
                + bankroll_before["realized_pnl"]
                + open_worst_after
            )
            risk_drawdown_after = max(
                self.config.quant_capital_usdc - risk_equity_after,
                Decimal("0"),
            )
            if (
                self.config.quant_max_drawdown_usdc > 0
                and risk_drawdown_after > self.config.quant_max_drawdown_usdc
            ):
                if self.config.dry_run:
                    dry_run_rejections.append("max_drawdown_candidate")
                else:
                    candidate_records.append(
                        {
                            "outcome": outcome,
                            "token_id": token_id,
                            "probability": probability.quantize(Decimal("0.0001")),
                            "best_ask": best_ask,
                            "raw_edge": raw_edge.quantize(Decimal("0.0001")),
                            "edge": edge.quantize(Decimal("0.0001")),
                            "limit_price": limit_price,
                            "size": size,
                            "notional": notional,
                            "max_notional": max_notional,
                            "risk_drawdown_after": risk_drawdown_after.quantize(Decimal("0.0001")),
                            "max_drawdown_usdc": self.config.quant_max_drawdown_usdc,
                            "position_after": lock_position_payload(position_after),
                            "bankroll_before": lock_bankroll_payload(bankroll_before),
                            "valid": False,
                            "skip_reason": "max_drawdown_candidate",
                        }
                    )
                    continue
            decision = QuantDecision(
                market=market,
                outcome=outcome,
                token_id=token_id,
                probability=probability.quantize(Decimal("0.0001")),
                best_ask=best_ask,
                raw_edge=raw_edge.quantize(Decimal("0.0001")),
                edge=edge.quantize(Decimal("0.0001")),
                limit_price=limit_price,
                size=size,
                reason=(
                    f"lock_model p({outcome})={probability.quantize(Decimal('0.0001'))} "
                    f"ask={best_ask} edge={edge.quantize(Decimal('0.0001'))}"
                ),
            )
            reason, score = self.score_lock_candidate(
                position_before,
                position_after,
                decision,
                would_lock,
                improvement,
            )
            model_rejections: List[str] = []
            if score and score[0] <= 0:
                model_rejections.append(reason)
            item = {
                "decision": decision,
                "legs": [decision],
                "reason": reason,
                "score": score,
                "notional": notional,
                "max_notional": max_notional,
                "position_after": position_after,
                "improvement_worst_pnl": improvement,
                "expected_pnl_before": expected_lock_pnl(position_before, probability if outcome == "Up" else Decimal("1") - probability),
                "expected_pnl_after": expected_lock_pnl(position_after, probability if outcome == "Up" else Decimal("1") - probability),
                "would_lock": would_lock,
                "dry_run_rejections": dry_run_rejections + model_rejections,
                "side_trade_count_before": side_trade_count_before,
                "side_trade_count_after": position_after[side_count_key],
                "side_trade_limit": MAX_LOCK_TRADES_PER_SIDE,
            }
            outcome_items[outcome] = item
            candidates.append(item)
            all_rejections = item.get("dry_run_rejections") or []
            candidate_record = lock_candidate_payload(item) | {"valid": not all_rejections}
            if all_rejections:
                candidate_record["would_skip_reasons"] = all_rejections
            candidate_records.append(candidate_record)
        arb_item = self.detect_pure_arbitrage(position_before, bankroll_before, outcome_items)
        if arb_item is not None:
            candidates.append(arb_item)
            dry_run_rejections = arb_item.get("dry_run_rejections") or []
            candidate_record = lock_candidate_payload(arb_item) | {"valid": not dry_run_rejections}
            if dry_run_rejections:
                candidate_record["would_skip_reasons"] = dry_run_rejections
            candidate_records.append(candidate_record)
        return candidates, candidate_records

    def detect_pure_arbitrage(
        self,
        position_before: Dict[str, Decimal],
        bankroll_before: Dict[str, Decimal],
        outcome_items: Dict[str, Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        up_item = outcome_items.get("Up")
        down_item = outcome_items.get("Down")
        if up_item is None or down_item is None:
            return None
        up_decision = up_item.get("decision")
        down_decision = down_item.get("decision")
        if not isinstance(up_decision, QuantDecision) or not isinstance(down_decision, QuantDecision):
            return None
        if (
            position_before["up_trade_count"] >= MAX_LOCK_TRADES_PER_SIDE
            or position_before["down_trade_count"] >= MAX_LOCK_TRADES_PER_SIDE
        ):
            return None
        sum_ask = up_decision.best_ask + down_decision.best_ask
        arb_edge = Decimal("1") - sum_ask
        if sum_ask >= Decimal("1") - self.config.quant_arbitrage_min_profit:
            return None
        size = min(up_decision.size, down_decision.size).quantize(Decimal("0.000001"), rounding=ROUND_FLOOR)
        if size <= 0:
            return None
        sum_fill = up_decision.best_ask + down_decision.best_ask
        notional = (size * sum_fill).quantize(Decimal("0.0001"))
        max_notional = notional
        market_cost_after = position_before["total_cost"] + notional
        dry_run_rejections: List[str] = []
        if market_cost_after > self.config.quant_market_max_usdc:
            if self.config.dry_run:
                dry_run_rejections.append("market_cap_reached")
            else:
                return None
        if max_notional > bankroll_before["available_to_add"]:
            if self.config.dry_run:
                dry_run_rejections.append("bankroll_cap_reached")
            else:
                return None
        up_leg = QuantDecision(
            market=up_decision.market,
            outcome=up_decision.outcome,
            token_id=up_decision.token_id,
            probability=up_decision.probability,
            best_ask=up_decision.best_ask,
            raw_edge=up_decision.raw_edge,
            edge=up_decision.edge,
            limit_price=up_decision.best_ask,
            size=size,
            reason="pure_arbitrage_up_leg",
        )
        down_leg = QuantDecision(
            market=down_decision.market,
            outcome=down_decision.outcome,
            token_id=down_decision.token_id,
            probability=down_decision.probability,
            best_ask=down_decision.best_ask,
            raw_edge=down_decision.raw_edge,
            edge=down_decision.edge,
            limit_price=down_decision.best_ask,
            size=size,
            reason="pure_arbitrage_down_leg",
        )
        position_after_up = lock_position_after_trade(position_before, "Up", size, up_leg.best_ask)
        position_after = lock_position_after_trade(position_after_up, "Down", size, down_leg.best_ask)
        expected_worst_profit = position_after["worst_pnl"] - position_before["worst_pnl"]
        return {
            "decision": up_leg,
            "legs": [up_leg, down_leg],
            "reason": "pure_arbitrage",
            "score": (Decimal("5"), expected_worst_profit, arb_edge, -notional),
            "notional": notional,
            "max_notional": max_notional,
            "position_after": position_after,
            "improvement_worst_pnl": expected_worst_profit,
            "would_lock": position_after["worst_pnl"] >= self.config.quant_lock_min_profit,
            "sum_ask": sum_ask,
            "arbitrage_edge": arb_edge,
            "dry_run_rejections": dry_run_rejections,
        }

    def score_lock_candidate(
        self,
        position_before: Dict[str, Decimal],
        position_after: Dict[str, Decimal],
        decision: QuantDecision,
        would_lock: bool,
        improvement: Decimal,
    ) -> Tuple[str, Tuple[Decimal, ...]]:
        p_up = decision.probability if decision.outcome == "Up" else Decimal("1") - decision.probability
        expected_before = expected_lock_pnl(position_before, p_up)
        expected_after = expected_lock_pnl(position_after, p_up)
        expected_gain = expected_after - expected_before
        notional = max(position_after["total_cost"] - position_before["total_cost"], Decimal("0.000001"))
        expected_efficiency = expected_gain / notional
        pnl_gap_before = abs(position_before["up_pnl"] - position_before["down_pnl"])
        pnl_gap = abs(position_after["up_pnl"] - position_after["down_pnl"])
        gap_improvement = pnl_gap_before - pnl_gap
        favorite_outcome = "Up" if p_up >= Decimal("0.5") else "Down"
        is_favorite = decision.outcome == favorite_outcome
        strong_market_momentum = decision.best_ask >= Decimal("0.65")
        cheap_inventory_hedge = decision.best_ask <= Decimal("0.35") and improvement > 0
        expected_positive = expected_gain > Decimal("0")
        if decision.best_ask <= Decimal("0.05") and not self.is_rebalance_side(position_before, decision.outcome):
            return (
                "cheap_side_without_inventory",
                (
                    Decimal("0"),
                    expected_after,
                    expected_gain,
                    improvement,
                    decision.edge,
                    -pnl_gap,
                    -position_after["total_cost"],
                ),
            )
        if would_lock:
            return (
                "lock_profit",
                (
                    Decimal("5"),
                    position_after["worst_pnl"],
                    expected_after,
                    -pnl_gap,
                    improvement,
                    decision.edge,
                    -position_after["total_cost"],
                ),
            )
        if position_before["trade_count"] == 0:
            if strong_market_momentum:
                return (
                    "initial_market_momentum",
                    (
                        Decimal("4.5"),
                        decision.best_ask,
                        expected_after,
                        decision.edge,
                        -pnl_gap,
                        -position_after["total_cost"],
                    ),
                )
            if not is_favorite and decision.edge < Decimal("0"):
                return (
                    "initial_wrong_side",
                    (Decimal("0"), expected_after, decision.edge, -pnl_gap, -position_after["total_cost"]),
                )
            return (
                "initial_momentum_probe",
                (
                    Decimal("3"),
                    expected_after,
                    decision.edge,
                    decision.probability,
                    -pnl_gap,
                    -position_after["total_cost"],
                ),
            )
        if strong_market_momentum:
            return (
                "momentum_follow",
                (
                    Decimal("4.5"),
                    decision.best_ask,
                    expected_after,
                    decision.probability,
                    decision.edge,
                    -pnl_gap,
                    -position_after["total_cost"],
                ),
            )
        if expected_positive and (is_favorite or decision.edge >= self.config.quant_min_edge):
            return (
                "expected_value_add",
                (
                    Decimal("4"),
                    expected_after,
                    expected_efficiency,
                    decision.edge,
                    -pnl_gap,
                    -position_after["total_cost"],
                ),
            )
        if (
            self.is_rebalance_side(position_before, decision.outcome)
            and improvement > 0
            and gap_improvement > 0
            and expected_gain >= -(notional * Decimal("0.20"))
        ):
            return (
                "rebalance_worst_side",
                (
                    Decimal("2"),
                    improvement,
                    gap_improvement,
                    expected_after,
                    -pnl_gap,
                    -position_after["total_cost"],
                ),
            )
        if cheap_inventory_hedge and expected_gain >= -(notional * Decimal("0.35")):
            return (
                "cheap_inventory_hedge",
                (
                    Decimal("1"),
                    improvement,
                    expected_after,
                    -pnl_gap,
                    decision.edge,
                    -position_after["total_cost"],
                ),
            )
        return (
            "weak_candidate",
            (
                Decimal("0"),
                expected_after,
                expected_gain,
                improvement,
                decision.edge,
                -pnl_gap,
                -position_after["total_cost"],
            ),
        )

    def detect_inventory_lock(
        self,
        position_before: Dict[str, Decimal],
        position_after: Dict[str, Decimal],
    ) -> bool:
        return position_before["trade_count"] > 0 and position_after["worst_pnl"] > position_before["worst_pnl"]

    def is_rebalance_side(self, position_before: Dict[str, Decimal], outcome: str) -> bool:
        if position_before["trade_count"] == 0:
            return False
        if position_before["up_pnl"] < position_before["down_pnl"]:
            return outcome == "Up"
        if position_before["down_pnl"] < position_before["up_pnl"]:
            return outcome == "Down"
        if position_before["up_size"] < position_before["down_size"]:
            return outcome == "Up"
        if position_before["down_size"] < position_before["up_size"]:
            return outcome == "Down"
        return False

    def select_lock_candidate(
        self,
        candidates: List[Dict[str, Any]],
        include_rejected: bool = False,
    ) -> Optional[Dict[str, Any]]:
        usable = [
            item
            for item in candidates
            if isinstance(item.get("score"), tuple)
            and (include_rejected or item["score"][0] > 0)
            and (include_rejected or not item.get("dry_run_rejections"))
        ]
        if not usable:
            return None
        return max(usable, key=lambda item: item["score"])

    def record_lock_signal(
        self,
        market: QuantMarket,
        snapshot: BtcSnapshot,
        action: str,
        reason: str,
        candidates: List[Dict[str, Any]],
        position_before: Dict[str, Decimal],
        position_after: Dict[str, Decimal],
        bankroll_before: Dict[str, Decimal],
        bankroll_after: Dict[str, Decimal],
        selected: Optional[QuantDecision] = None,
        selected_meta: Optional[Dict[str, Any]] = None,
        market_snapshot: Optional[Dict[str, Any]] = None,
        force: bool = False,
    ) -> None:
        now = datetime.now(timezone.utc)
        if market_snapshot is None:
            market_snapshot = self.lock_loop_snapshot(market, position_before)
        record: Dict[str, Any] = {
            "ts": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "unix_ts": int(now.timestamp()),
            "bot_mode": "quant",
            "quant_strategy": "lock",
            "dry_run": self.config.dry_run,
            "action": action,
            "reason": reason,
            "market": {
                "slug": market.slug,
                "title": market.title,
                "start_ts": market.start_ts,
                "end_ts": market.end_ts,
                "seconds_left": market.seconds_left,
                "outcomes": market.outcomes,
                "token_ids": market.token_ids,
            },
            "price": {
                "source": snapshot.source,
                "symbol": self.config.quant_symbol,
                "current_ts": snapshot.current_ts,
                "current": snapshot.current,
                "start_price_ts": snapshot.start_price_ts,
                "start_price": snapshot.start_price,
                "ret_from_start": snapshot.ret_from_start,
                "ret_1m": snapshot.ret_1m,
                "ret_3m": snapshot.ret_3m,
                "up_probability": snapshot.up_probability,
            },
            "position_before": lock_position_payload(position_before),
            "position_after": lock_position_payload(position_after),
            "bankroll_before": lock_bankroll_payload(bankroll_before),
            "bankroll_after": lock_bankroll_payload(bankroll_after),
            "market_snapshot": market_snapshot,
            "config": {
                "size_mode": self.config.quant_size_mode,
                "order_usdc": self.config.quant_order_usdc,
                "order_shares": self.config.quant_order_shares,
                "capital_usdc": self.config.quant_capital_usdc,
                "market_max_usdc": self.config.quant_market_max_usdc,
                "max_trades_per_market": self.config.quant_max_trades_per_market,
                "rebuy_cooldown_sec": self.config.quant_rebuy_cooldown_sec,
                "lock_min_profit": self.config.quant_lock_min_profit,
                "lock_stop_on_lock": self.config.quant_lock_stop_on_lock,
                "arbitrage_min_profit": self.config.quant_arbitrage_min_profit,
                "min_edge": self.config.quant_min_edge,
                "min_seconds_left": self.config.quant_min_seconds_left,
                "max_seconds_left": self.config.quant_max_seconds_left,
                "max_drawdown_usdc": self.config.quant_max_drawdown_usdc,
                "max_slippage": self.config.max_slippage,
            },
            "candidates": candidates,
            "selected": self.decision_payload(selected) if selected else None,
            "selected_meta": selected_meta,
        }
        self.signal_recorder.write(json_safe(record), force=force)

    def record_signal(
        self,
        market: QuantMarket,
        snapshot: BtcSnapshot,
        action: str,
        reason: str,
        candidates: List[Dict[str, Any]],
        selected: Optional[QuantDecision] = None,
        force: bool = False,
    ) -> None:
        now = datetime.now(timezone.utc)
        record: Dict[str, Any] = {
            "ts": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "unix_ts": int(now.timestamp()),
            "bot_mode": "quant",
            "dry_run": self.config.dry_run,
            "action": action,
            "reason": reason,
            "market": {
                "slug": market.slug,
                "title": market.title,
                "start_ts": market.start_ts,
                "end_ts": market.end_ts,
                "seconds_left": market.seconds_left,
                "outcomes": market.outcomes,
                "token_ids": market.token_ids,
            },
            "price": {
                "source": snapshot.source,
                "symbol": self.config.quant_symbol,
                "current_ts": snapshot.current_ts,
                "current": snapshot.current,
                "start_price_ts": snapshot.start_price_ts,
                "start_price": snapshot.start_price,
                "ret_from_start": snapshot.ret_from_start,
                "ret_1m": snapshot.ret_1m,
                "ret_3m": snapshot.ret_3m,
                "up_probability": snapshot.up_probability,
            },
            "market_snapshot": self.lock_loop_snapshot(market, base_lock_position()),
            "config": {
                "strategy": self.config.quant_strategy,
                "size_mode": self.config.quant_size_mode,
                "order_usdc": self.config.quant_order_usdc,
                "order_shares": self.config.quant_order_shares,
                "min_edge": self.config.quant_min_edge,
                "min_seconds_left": self.config.quant_min_seconds_left,
                "max_seconds_left": self.config.quant_max_seconds_left,
                "max_slippage": self.config.max_slippage,
            },
            "candidates": candidates,
            "selected": self.decision_payload(selected) if selected else None,
        }
        self.signal_recorder.write(json_safe(record), force=force)

    def decision_payload(self, decision: Optional[QuantDecision]) -> Dict[str, Any]:
        if decision is None:
            return {}
        return {
            "outcome": decision.outcome,
            "token_id": decision.token_id,
            "probability": decision.probability,
            "best_ask": decision.best_ask,
            "raw_edge": decision.raw_edge,
            "edge": decision.edge,
            "limit_price": decision.limit_price,
            "size": decision.size,
            "reason": decision.reason,
        }

    def log_throttled(self, message: str) -> None:
        if time.time() - self.last_log_ts >= self.config.quant_log_interval_sec:
            print(message)
            self.last_log_ts = time.time()


def state_decimal(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def base_lock_position() -> Dict[str, Decimal]:
    return {
        "up_size": Decimal("0"),
        "down_size": Decimal("0"),
        "up_cost": Decimal("0"),
        "down_cost": Decimal("0"),
        "total_cost": Decimal("0"),
        "up_avg_price": Decimal("0"),
        "down_avg_price": Decimal("0"),
        "up_pnl": Decimal("0"),
        "down_pnl": Decimal("0"),
        "worst_pnl": Decimal("0"),
        "best_pnl": Decimal("0"),
        "trade_count": Decimal("0"),
        "up_trade_count": Decimal("0"),
        "down_trade_count": Decimal("0"),
    }


def recalc_lock_position(position: Dict[str, Decimal]) -> Dict[str, Decimal]:
    up_size = position["up_size"]
    down_size = position["down_size"]
    up_cost = position["up_cost"]
    down_cost = position["down_cost"]
    total_cost = up_cost + down_cost
    position["total_cost"] = total_cost
    position["up_avg_price"] = up_cost / up_size if up_size > 0 else Decimal("0")
    position["down_avg_price"] = down_cost / down_size if down_size > 0 else Decimal("0")
    position["up_pnl"] = up_size - total_cost
    position["down_pnl"] = down_size - total_cost
    position["worst_pnl"] = min(position["up_pnl"], position["down_pnl"])
    position["best_pnl"] = max(position["up_pnl"], position["down_pnl"])
    return position


def expected_lock_pnl(position: Dict[str, Decimal], up_probability: Decimal) -> Decimal:
    up_probability = min(max(up_probability, Decimal("0")), Decimal("1"))
    return position["up_pnl"] * up_probability + position["down_pnl"] * (Decimal("1") - up_probability)


def lock_position_from_entry(entry: Dict[str, Any]) -> Dict[str, Decimal]:
    position = base_lock_position()
    trades = entry.get("trades")
    valid_count = 0
    if not isinstance(trades, list):
        return recalc_lock_position(position)
    for trade in trades:
        if not isinstance(trade, dict):
            continue
        outcome = str(trade.get("outcome") or "")
        size = state_decimal(trade.get("size"))
        price = state_decimal(trade.get("limit_price"))
        cost = state_decimal(trade.get("cost"))
        if cost <= 0:
            cost = size * price
        if size <= 0 or cost <= 0:
            continue
        if outcome == "Up":
            position["up_size"] += size
            position["up_cost"] += cost
            position["up_trade_count"] += Decimal("1")
        elif outcome == "Down":
            position["down_size"] += size
            position["down_cost"] += cost
            position["down_trade_count"] += Decimal("1")
        else:
            continue
        valid_count += 1
    position["trade_count"] = Decimal(valid_count)
    return recalc_lock_position(position)


def lock_position_after_trade(
    position_before: Dict[str, Decimal],
    outcome: str,
    size: Decimal,
    price: Decimal,
) -> Dict[str, Decimal]:
    position = {key: Decimal(value) for key, value in position_before.items()}
    cost = size * price
    if outcome == "Up":
        position["up_size"] += size
        position["up_cost"] += cost
        position["up_trade_count"] += Decimal("1")
    else:
        position["down_size"] += size
        position["down_cost"] += cost
        position["down_trade_count"] += Decimal("1")
    position["trade_count"] += Decimal("1")
    return recalc_lock_position(position)


def same_outcome_repeat_skip(
    entry: Optional[Dict[str, Any]],
    outcome: str,
    best_ask: Decimal,
) -> str:
    if not isinstance(entry, dict):
        return ""
    trades = entry.get("trades")
    if not isinstance(trades, list) or not trades:
        return ""
    for trade in reversed(trades):
        if not isinstance(trade, dict):
            continue
        if str(trade.get("outcome") or "") != outcome:
            return ""
        last_ts = int(state_decimal(trade.get("ts")))
        if last_ts <= 0 or time.time() - last_ts >= SAME_OUTCOME_REPEAT_SEC:
            return ""
        last_price = state_decimal(trade.get("limit_price"))
        if abs(best_ask - last_price) >= SAME_OUTCOME_MIN_PRICE_MOVE:
            return ""
        return "same_outcome_no_price_move"
    return ""


def lock_position_payload(position: Dict[str, Decimal]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for key, value in position.items():
        if key in {"trade_count", "up_trade_count", "down_trade_count"}:
            payload[key] = int(value)
        else:
            payload[key] = str(value.quantize(Decimal("0.0001")))
    return payload


def lock_bankroll_payload(bankroll: Dict[str, Decimal]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    integer_keys = {"unsettled_markets", "settled_markets"}
    for key, value in bankroll.items():
        if key in integer_keys:
            payload[key] = int(value)
        else:
            payload[key] = str(value.quantize(Decimal("0.0001")))
    return payload


def lock_candidate_payload(item: Dict[str, Any]) -> Dict[str, Any]:
    decision = item.get("decision")
    if not isinstance(decision, QuantDecision):
        return {}
    score = item.get("score")
    legs = item.get("legs")
    leg_payloads: List[Dict[str, Any]] = []
    if isinstance(legs, list):
        leg_payloads = [decision_payload_from_dataclass(leg) for leg in legs if isinstance(leg, QuantDecision)]
    return {
        "outcome": decision.outcome,
        "token_id": decision.token_id,
        "probability": decision.probability,
        "best_ask": decision.best_ask,
        "raw_edge": decision.raw_edge,
        "edge": decision.edge,
        "limit_price": decision.limit_price,
        "size": decision.size,
        "notional": item.get("notional"),
        "max_notional": item.get("max_notional"),
        "reason": item.get("reason"),
        "improvement_worst_pnl": item.get("improvement_worst_pnl"),
        "expected_pnl_before": item.get("expected_pnl_before"),
        "expected_pnl_after": item.get("expected_pnl_after"),
        "would_lock": bool(item.get("would_lock")),
        "score": list(score) if isinstance(score, tuple) else score,
        "position_after": lock_position_payload(item.get("position_after", base_lock_position())),
        "legs": leg_payloads,
        "sum_ask": item.get("sum_ask"),
        "arbitrage_edge": item.get("arbitrage_edge"),
        "dry_run_rejections": item.get("dry_run_rejections", []),
        "side_trade_count_before": item.get("side_trade_count_before"),
        "side_trade_count_after": item.get("side_trade_count_after"),
        "side_trade_limit": item.get("side_trade_limit"),
    }


def decision_payload_from_dataclass(decision: QuantDecision) -> Dict[str, Any]:
    return {
        "outcome": decision.outcome,
        "token_id": decision.token_id,
        "probability": decision.probability,
        "best_ask": decision.best_ask,
        "raw_edge": decision.raw_edge,
        "edge": decision.edge,
        "limit_price": decision.limit_price,
        "size": decision.size,
        "reason": decision.reason,
    }


def json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [json_safe(item) for item in value]
    return value


def parse_json_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def parse_okx_candles(rows: List[Any]) -> List[Dict[str, Decimal]]:
    candles: List[Dict[str, Decimal]] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 5:
            continue
        candles.append(
            {
                "ts": int(Decimal(str(row[0])) / Decimal("1000")),
                "open": decimal_value(row[1]),
                "high": decimal_value(row[2]),
                "low": decimal_value(row[3]),
                "close": decimal_value(row[4]),
            }
        )
    if not candles:
        raise RuntimeError("OKX candles 为空")
    return candles


def nearest_candle_open(candles: List[Dict[str, Decimal]], target_ts: int) -> Decimal:
    nearest = min(candles, key=lambda candle: abs(int(candle["ts"]) - target_ts))
    return nearest["open"]


def nearest_candle_ts(candles: List[Dict[str, Decimal]], target_ts: int) -> int:
    nearest = min(candles, key=lambda candle: abs(int(candle["ts"]) - target_ts))
    return int(nearest["ts"])


def nearest_price(prices: List[Tuple[int, Decimal]], target_ts: int) -> Optional[Tuple[int, Decimal]]:
    if not prices:
        return None
    return min(prices, key=lambda item: abs(item[0] - target_ts))


def decimal_return(current: Decimal, previous: Decimal) -> Decimal:
    if previous <= 0:
        return Decimal("0")
    return (current - previous) / previous


def estimate_up_probability(ret_from_start: Decimal, ret_1m: Decimal, ret_3m: Decimal) -> Decimal:
    score = (
        float(ret_from_start) * 180.0
        + float(ret_1m) * 45.0
        + float(ret_3m) * 25.0
    )
    probability = 1.0 / (1.0 + math.exp(-score))
    probability = min(max(probability, 0.05), 0.95)
    return Decimal(str(probability)).quantize(Decimal("0.0001"))


def iso_to_ts(value: str) -> int:
    clean = value.replace("Z", "+00:00")
    return int(datetime.fromisoformat(clean).astimezone(timezone.utc).timestamp())
