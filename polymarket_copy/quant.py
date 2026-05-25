from __future__ import annotations

import json
import math
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
    current: Decimal
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
    edge: Decimal
    limit_price: Decimal
    size: Decimal
    reason: str


class QuantStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.state: Dict[str, Any] = {"markets": {}, "last_order_ts": 0}

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


class PolymarketQuantBot:
    def __init__(self, config: CopyBotConfig) -> None:
        self.config = config
        self.http = HttpJsonClient(config)
        self.market_ws = MarketWsBookCache(config)
        self.book_cache = BookCache(self.http, config, self.market_ws)
        self.order_helper = PolymarketCopyBot(config)
        self.state = QuantStateStore(config.quant_state_file)
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
            f"order_usdc={self.config.quant_order_usdc} "
            f"min_edge={self.config.quant_min_edge} "
            f"min_seconds_left={self.config.quant_min_seconds_left}"
        )

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

    def run_once(self, client: Any = None) -> Optional[QuantDecision]:
        market = self.find_current_market()
        if not market:
            self.log_throttled("[AI QUANT] 未找到当前 BTC 5m 市场，等待下一轮。")
            return None

        self.market_ws.start(market.token_ids)
        snapshot = self.fetch_btc_snapshot(market.start_ts)
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
        if self.config.quant_price_source != "okx":
            raise RuntimeError("第一版 AI 量化只支持 QUANT_PRICE_SOURCE=okx")

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
            current=current,
            start_price=start_price,
            ret_from_start=ret_from_start,
            ret_1m=ret_1m,
            ret_3m=ret_3m,
            up_probability=up_probability,
        )

    def make_decision(self, market: QuantMarket, snapshot: BtcSnapshot) -> Optional[QuantDecision]:
        if self.state.has_market(market.slug):
            self.log_throttled(f"[AI QUANT] {market.slug} 已有决策记录，本窗口不重复下单。")
            return None
        if not self.state.cooldown_ready(self.config.quant_cooldown_sec):
            self.log_throttled("[AI QUANT] 冷却中，暂不下单。")
            return None
        if market.seconds_left < self.config.quant_min_seconds_left:
            self.log_throttled(
                f"[AI QUANT] {market.title} 剩余 {market.seconds_left}s，低于最小剩余时间，跳过。"
            )
            return None

        candidates: List[QuantDecision] = []
        for outcome, token_id in zip(market.outcomes, market.token_ids):
            probability = snapshot.up_probability if outcome == "Up" else Decimal("1") - snapshot.up_probability
            book = self.book_cache.get_book(token_id)
            best_ask = best_book_price(book, "BUY")
            if best_ask is None:
                continue
            edge = probability - best_ask
            tick_size = decimal_value(book.get("tick_size", "0.01"))
            limit_price = protected_limit_price("BUY", best_ask, tick_size, self.config.max_slippage)
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
                continue
            candidates.append(
                QuantDecision(
                    market=market,
                    outcome=outcome,
                    token_id=token_id,
                    probability=probability.quantize(Decimal("0.0001")),
                    best_ask=best_ask,
                    edge=edge.quantize(Decimal("0.0001")),
                    limit_price=limit_price,
                    size=size,
                    reason=(
                        f"p({outcome})={probability.quantize(Decimal('0.0001'))} "
                        f"> ask={best_ask} + edge={edge.quantize(Decimal('0.0001'))}"
                    ),
                )
            )

        if not candidates:
            self.log_throttled("[AI QUANT] 没有可用盘口候选。")
            return None

        best = max(candidates, key=lambda item: item.edge)
        if best.edge < self.config.quant_min_edge:
            self.log_throttled(
                "[AI QUANT] "
                f"{market.title} p_up={snapshot.up_probability} "
                f"best={best.outcome} edge={best.edge} < min_edge={self.config.quant_min_edge}，跳过。"
            )
            return None
        return best

    def log_throttled(self, message: str) -> None:
        if time.time() - self.last_log_ts >= self.config.quant_log_interval_sec:
            print(message)
            self.last_log_ts = time.time()


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
