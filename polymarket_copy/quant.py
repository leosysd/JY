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
        self.signal_recorder = QuantSignalRecorder(config)
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
        if self.config.quant_record_signals:
            print(f"[AI QUANT DATA] signals={self.config.quant_signal_file}")

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
                candidate_records.append(
                    {
                        "outcome": outcome,
                        "token_id": token_id,
                        "probability": probability.quantize(Decimal("0.0001")),
                        "best_ask": best_ask,
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
                "current": snapshot.current,
                "start_price": snapshot.start_price,
                "ret_from_start": snapshot.ret_from_start,
                "ret_1m": snapshot.ret_1m,
                "ret_3m": snapshot.ret_3m,
                "up_probability": snapshot.up_probability,
            },
            "config": {
                "order_usdc": self.config.quant_order_usdc,
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
            "edge": decision.edge,
            "limit_price": decision.limit_price,
            "size": decision.size,
            "reason": decision.reason,
        }

    def log_throttled(self, message: str) -> None:
        if time.time() - self.last_log_ts >= self.config.quant_log_interval_sec:
            print(message)
            self.last_log_ts = time.time()


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
