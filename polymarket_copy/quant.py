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
    protected_limit_price,
)
from .config import CopyBotConfig, validate_config


GAMMA_API = "https://gamma-api.polymarket.com"
OKX_CANDLES_URL = "https://www.okx.com/api/v5/market/history-candles"


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
                "seconds_left": market.seconds_left,
            }
        )
        entry["locked"] = bool(locked_after)
        entry["last_trade_ts"] = now
        entry["position"] = lock_position_payload(position_after)
        self.state["last_order_ts"] = now
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
            f"min_edge={self.config.quant_min_edge} "
            f"min_seconds_left={self.config.quant_min_seconds_left}"
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
            return self.make_lock_decision(market, snapshot)

        decision = self.make_decision(market, snapshot)
        if decision is None:
            return None

        mode = "DRY_RUN" if self.config.dry_run else "LIVE"
        print(
            "[AI QUANT BUY] "
            f"mode={mode} market={decision.market.title} outcome={decision.outcome} "
            f"prob={decision.probability} best_ask={decision.best_ask} "
            f"edge={decision.edge} limit={decision.limit_price} size={decision.size} "
            f"ret_start={snapshot.ret_from_start} ret_1m={snapshot.ret_1m} "
            f"seconds_left={market.seconds_left} reason={decision.reason}"
        )
        if self.config.dry_run:
            self.state.mark_market(decision, mode="dry_run")
            return decision

        book = self.book_cache.get_book(decision.token_id)
        tick_size = decimal_value(book.get("tick_size", "0.01"))
        neg_risk = bool(book.get("neg_risk", market.neg_risk))
        self.order_helper._post_order(  # noqa: SLF001 - shared local order helper
            client,
            decision.token_id,
            decision.limit_price,
            decision.size,
            "BUY",
            tick_size,
            neg_risk,
        )
        self.state.mark_market(decision, mode="live")
        return decision

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

    def make_decision(self, market: QuantMarket, snapshot: BtcSnapshot) -> Optional[QuantDecision]:
        if self.state.has_market(market.slug):
            self.log_throttled(f"[AI QUANT] {market.slug} 已有决策记录，本窗口不重复下单。")
            self.record_signal(market, snapshot, "skip", "market_already_decided", [])
            return None
        if not self.state.cooldown_ready(self.config.quant_cooldown_sec):
            self.log_throttled("[AI QUANT] 冷却中，暂不下单。")
            self.record_signal(market, snapshot, "skip", "cooldown", [])
            return None
        if market.seconds_left < self.config.quant_min_seconds_left:
            self.log_throttled(
                f"[AI QUANT] {market.title} 剩余 {market.seconds_left}s，低于最小剩余时间，跳过。"
            )
            self.record_signal(market, snapshot, "skip", "low_seconds_left", [])
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
            tick_size = decimal_value(book.get("tick_size", "0.01"))
            limit_price = protected_limit_price("BUY", best_ask, tick_size, self.config.max_slippage)
            raw_edge = probability - best_ask
            edge = probability - limit_price
            size = (self.config.quant_order_usdc / limit_price).quantize(
                Decimal("0.000001"),
                rounding=ROUND_FLOOR,
            )
            size = apply_max_order_usdc(size, limit_price, self.config.max_order_usdc)
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
                    f"> limit={limit_price} + effective_edge={edge.quantize(Decimal('0.0001'))}"
                ),
            )
            candidates.append(decision)
            candidate_records.append(self.decision_payload(decision) | {"valid": True})

        if not candidates:
            self.log_throttled("[AI QUANT] 没有可用盘口候选。")
            self.record_signal(market, snapshot, "skip", "no_valid_candidates", candidate_records)
            return None

        best = max(candidates, key=lambda item: item.edge)
        if best.edge < self.config.quant_min_edge:
            self.log_throttled(
                "[AI QUANT] "
                f"{market.title} p_up={snapshot.up_probability} "
                f"best={best.outcome} edge={best.edge} < min_edge={self.config.quant_min_edge}，跳过。"
            )
            self.record_signal(
                market,
                snapshot,
                "skip",
                "edge_below_min",
                candidate_records,
                selected=best,
            )
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

    def lock_bankroll_snapshot(self, current_slug: str, current_cost: Decimal) -> Dict[str, Decimal]:
        realized_pnl = Decimal("0")
        other_unsettled_cost = Decimal("0")
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
                unsettled_count += 1
        equity = self.config.quant_capital_usdc + realized_pnl
        available_to_add = equity - other_unsettled_cost - current_cost
        return {
            "capital_usdc": self.config.quant_capital_usdc,
            "realized_pnl": realized_pnl,
            "equity": equity,
            "other_unsettled_cost": other_unsettled_cost,
            "current_market_cost": current_cost,
            "available_to_add": max(available_to_add, Decimal("0")),
            "raw_available_to_add": available_to_add,
            "unsettled_markets": Decimal(unsettled_count),
            "settled_markets": Decimal(settled_count),
        }

    def make_lock_decision(self, market: QuantMarket, snapshot: BtcSnapshot) -> Optional[QuantDecision]:
        if not self.config.dry_run:
            raise RuntimeError("QUANT_STRATEGY=lock 第一版只允许 DRY_RUN=1，暂不接实盘下单")
        self.settle_finished_lock_markets()
        entry = self.state.lock_market_entry(market)
        position_before = lock_position_from_entry(entry)
        bankroll_before = self.lock_bankroll_snapshot(market.slug, position_before["total_cost"])
        if self.config.quant_lock_stop_on_lock and bool(entry.get("locked")):
            self.log_throttled(f"[AI LOCK] {market.slug} 已经锁利，停止本市场。")
            self.record_lock_signal(
                market,
                snapshot,
                action="skip",
                reason="market_locked",
                candidates=[],
                position_before=position_before,
                position_after=position_before,
                bankroll_before=bankroll_before,
                bankroll_after=bankroll_before,
            )
            return None
        if market.seconds_left < self.config.quant_min_seconds_left:
            self.log_throttled(
                f"[AI LOCK] {market.title} 剩余 {market.seconds_left}s，低于最小剩余时间，跳过。"
            )
            self.record_lock_signal(
                market,
                snapshot,
                action="skip",
                reason="low_seconds_left",
                candidates=[],
                position_before=position_before,
                position_after=position_before,
                bankroll_before=bankroll_before,
                bankroll_after=bankroll_before,
            )
            return None
        if position_before["trade_count"] >= Decimal(str(self.config.quant_max_trades_per_market)):
            self.log_throttled(f"[AI LOCK] {market.slug} 已达到单市场最多笔数，跳过。")
            self.record_lock_signal(
                market,
                snapshot,
                action="skip",
                reason="max_trades_reached",
                candidates=[],
                position_before=position_before,
                position_after=position_before,
                bankroll_before=bankroll_before,
                bankroll_after=bankroll_before,
            )
            return None
        last_trade_ts = float(entry.get("last_trade_ts") or 0)
        if last_trade_ts and time.time() - last_trade_ts < self.config.quant_rebuy_cooldown_sec:
            self.record_lock_signal(
                market,
                snapshot,
                action="skip",
                reason="rebuy_cooldown",
                candidates=[],
                position_before=position_before,
                position_after=position_before,
                bankroll_before=bankroll_before,
                bankroll_after=bankroll_before,
            )
            return None

        candidates, candidate_records = self.build_lock_candidates(market, snapshot, position_before, bankroll_before)
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
                action="skip",
                reason="no_lock_candidate",
                candidates=candidate_records,
                position_before=position_before,
                position_after=position_before,
                bankroll_before=bankroll_before,
                bankroll_after=bankroll_before,
            )
            return None

        decision = selected["decision"]
        position_after = selected["position_after"]
        bankroll_after = self.lock_bankroll_snapshot(market.slug, position_after["total_cost"])
        locked_after = position_after["worst_pnl"] >= self.config.quant_lock_min_profit
        reason = str(selected["reason"])
        print(
            "[AI LOCK BUY] "
            f"mode=DRY_RUN market={decision.market.title} outcome={decision.outcome} "
            f"size={decision.size} price={decision.limit_price} "
            f"cost={selected['notional']} edge={decision.edge} "
            f"up_pnl={position_after['up_pnl']} down_pnl={position_after['down_pnl']} "
            f"total_cost={position_after['total_cost']} trades={position_after['trade_count']} "
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
        self.state.append_lock_trade(
            market,
            decision,
            mode="dry_run",
            position_after=position_after,
            locked_after=locked_after,
            reason=reason,
        )
        return decision

    def build_lock_candidates(
        self,
        market: QuantMarket,
        snapshot: BtcSnapshot,
        position_before: Dict[str, Decimal],
        bankroll_before: Dict[str, Decimal],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        candidates: List[Dict[str, Any]] = []
        candidate_records: List[Dict[str, Any]] = []
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
            tick_size = decimal_value(book.get("tick_size", "0.01"))
            limit_price = protected_limit_price("BUY", best_ask, tick_size, self.config.max_slippage)
            raw_edge = probability - best_ask
            edge = probability - limit_price
            if self.config.quant_size_mode == "shares":
                size = self.config.quant_order_shares.quantize(Decimal("0.000001"), rounding=ROUND_FLOOR)
            else:
                size = (self.config.quant_order_usdc / limit_price).quantize(
                    Decimal("0.000001"),
                    rounding=ROUND_FLOOR,
                )
            size = apply_max_order_usdc(size, limit_price, self.config.max_order_usdc)
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
            notional = (size * limit_price).quantize(Decimal("0.0001"))
            if notional > budget_remaining:
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
                        "budget_remaining": budget_remaining,
                        "market_budget_remaining": market_budget_remaining,
                        "bankroll_before": lock_bankroll_payload(bankroll_before),
                        "position_before": lock_position_payload(position_before),
                        "valid": False,
                        "skip_reason": "budget_cap_reached",
                    }
                )
                continue

            position_after = lock_position_after_trade(position_before, outcome, size, limit_price)
            improvement = position_after["worst_pnl"] - position_before["worst_pnl"]
            would_lock = position_after["worst_pnl"] >= self.config.quant_lock_min_profit
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
                    f"limit={limit_price} effective_edge={edge.quantize(Decimal('0.0001'))}"
                ),
            )
            reason, score = self.score_lock_candidate(
                position_before,
                position_after,
                decision,
                would_lock,
                improvement,
            )
            item = {
                "decision": decision,
                "reason": reason,
                "score": score,
                "notional": notional,
                "position_after": position_after,
                "improvement_worst_pnl": improvement,
                "would_lock": would_lock,
            }
            candidates.append(item)
            candidate_records.append(lock_candidate_payload(item) | {"valid": True})
        return candidates, candidate_records

    def score_lock_candidate(
        self,
        position_before: Dict[str, Decimal],
        position_after: Dict[str, Decimal],
        decision: QuantDecision,
        would_lock: bool,
        improvement: Decimal,
    ) -> Tuple[str, Tuple[Decimal, Decimal, Decimal, Decimal]]:
        if would_lock:
            return (
                "lock_profit",
                (Decimal("4"), position_after["worst_pnl"], improvement, decision.edge),
            )
        if improvement > 0:
            return (
                "improve_worst_pnl",
                (Decimal("3"), improvement, decision.edge, decision.probability),
            )
        if position_before["trade_count"] == 0:
            return (
                "initial_probe",
                (Decimal("2"), decision.edge, decision.probability, -position_after["total_cost"]),
            )
        if decision.edge >= self.config.quant_min_edge:
            return (
                "add_same_side_edge",
                (Decimal("1"), decision.edge, decision.probability, -position_after["total_cost"]),
            )
        return (
            "weak_candidate",
            (Decimal("0"), decision.edge, improvement, -position_after["total_cost"]),
        )

    def select_lock_candidate(self, candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        usable = [
            item
            for item in candidates
            if isinstance(item.get("score"), tuple)
            and item["score"][0] > 0
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
        force: bool = False,
    ) -> None:
        now = datetime.now(timezone.utc)
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
                "min_edge": self.config.quant_min_edge,
                "min_seconds_left": self.config.quant_min_seconds_left,
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
            "config": {
                "strategy": self.config.quant_strategy,
                "size_mode": self.config.quant_size_mode,
                "order_usdc": self.config.quant_order_usdc,
                "order_shares": self.config.quant_order_shares,
                "min_edge": self.config.quant_min_edge,
                "min_seconds_left": self.config.quant_min_seconds_left,
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
        elif outcome == "Down":
            position["down_size"] += size
            position["down_cost"] += cost
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
    else:
        position["down_size"] += size
        position["down_cost"] += cost
    position["trade_count"] += Decimal("1")
    return recalc_lock_position(position)


def lock_position_payload(position: Dict[str, Decimal]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for key, value in position.items():
        if key == "trade_count":
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
        "reason": item.get("reason"),
        "improvement_worst_pnl": item.get("improvement_worst_pnl"),
        "would_lock": bool(item.get("would_lock")),
        "score": list(score) if isinstance(score, tuple) else score,
        "position_after": lock_position_payload(item.get("position_after", base_lock_position())),
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
