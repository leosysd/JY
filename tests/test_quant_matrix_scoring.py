from __future__ import annotations

import time
import unittest
from decimal import Decimal

from polymarket_copy.config import CopyBotConfig
from polymarket_copy.quant import (
    PolymarketQuantBot,
    QuantDecision,
    QuantMarket,
    base_lock_position,
    lock_position_after_trade,
    recalc_lock_position,
)


def position(**updates):
    current = base_lock_position()
    current.update({key: Decimal(str(value)) for key, value in updates.items()})
    return recalc_lock_position(current)


def decision(
    outcome: str,
    probability: str,
    price: str,
    total_cost: str,
    elapsed_sec: int = 120,
) -> QuantDecision:
    now = int(time.time())
    market = QuantMarket(
        slug="btc-updown-5m-test",
        title="test market",
        start_ts=now - elapsed_sec,
        end_ts=now + max(1, 300 - elapsed_sec),
        outcomes=["Up", "Down"],
        token_ids=["up-token", "down-token"],
        neg_risk=False,
    )
    return QuantDecision(
        market=market,
        outcome=outcome,
        token_id=f"{outcome.lower()}-token",
        probability=Decimal(probability),
        best_ask=Decimal(price),
        raw_edge=Decimal(probability) - Decimal(price),
        edge=Decimal(probability) - Decimal(total_cost) / Decimal("20"),
        limit_price=Decimal(price),
        size=Decimal("20"),
        reason="test",
        vwap_price=Decimal(price),
        effective_price=(Decimal(total_cost) / Decimal("20")).quantize(Decimal("0.0001")),
        notional=Decimal(total_cost),
        fee=Decimal("0"),
        total_cost=Decimal(total_cost),
        liquidity_levels=1,
        depth_available=Decimal("100"),
        fill_complete=True,
    )


class QuantMatrixScoringTest(unittest.TestCase):
    def setUp(self) -> None:
        self.bot = PolymarketQuantBot.__new__(PolymarketQuantBot)
        self.bot.config = CopyBotConfig(quant_lock_min_profit=Decimal("0.20"), quant_min_edge=Decimal("0.02"))

    def test_directional_follow_cannot_widen_existing_matrix_loss(self) -> None:
        before = position(
            up_size="60",
            down_size="20",
            up_cost="36",
            down_cost="10",
            up_trade_count="3",
            down_trade_count="1",
            trade_count="4",
        )
        chase_up = decision("Up", "0.65", "0.60", "12")
        after = lock_position_after_trade(
            before,
            chase_up.outcome,
            chase_up.size,
            chase_up.limit_price,
            chase_up.notional,
            chase_up.fee,
            chase_up.total_cost,
        )

        reason, score = self.bot.score_lock_candidate(
            before,
            after,
            chase_up,
            would_lock=False,
            improvement=after["worst_pnl"] - before["worst_pnl"],
            btc_flow={"available": True, "outcome": "Up", "confidence": Decimal("0.9")},
        )

        self.assertEqual(reason, "matrix_would_widen_loss")
        self.assertEqual(score[0], Decimal("0"))

    def test_rebalance_side_still_gets_positive_matrix_score(self) -> None:
        before = position(
            up_size="60",
            down_size="20",
            up_cost="36",
            down_cost="10",
            up_trade_count="3",
            down_trade_count="1",
            trade_count="4",
        )
        repair_down = decision("Down", "0.58", "0.35", "7")
        after = lock_position_after_trade(
            before,
            repair_down.outcome,
            repair_down.size,
            repair_down.limit_price,
            repair_down.notional,
            repair_down.fee,
            repair_down.total_cost,
        )

        reason, score = self.bot.score_lock_candidate(
            before,
            after,
            repair_down,
            would_lock=False,
            improvement=after["worst_pnl"] - before["worst_pnl"],
            btc_flow={"available": True, "outcome": "Down", "confidence": Decimal("0.9")},
        )

        self.assertIn(reason, {"matrix_risk_reduction", "jet_matrix_rebalance", "target_rebalance_ladder"})
        self.assertGreater(score[0], Decimal("0"))
        self.assertGreater(after["worst_pnl"], before["worst_pnl"])

    def test_initial_probe_waits_during_observation_window(self) -> None:
        before = position()
        early_up = decision("Up", "0.54", "0.50", "10", elapsed_sec=5)
        after = lock_position_after_trade(
            before,
            early_up.outcome,
            early_up.size,
            early_up.limit_price,
            early_up.notional,
            early_up.fee,
            early_up.total_cost,
        )

        reason, score = self.bot.score_lock_candidate(
            before,
            after,
            early_up,
            would_lock=False,
            improvement=after["worst_pnl"] - before["worst_pnl"],
            btc_flow={"available": False},
        )

        self.assertEqual(reason, "initial_observation_wait")
        self.assertEqual(score[0], Decimal("0"))

    def test_initial_probe_waits_on_early_high_price_chase(self) -> None:
        before = position()
        high_up = decision("Up", "0.62", "0.70", "14", elapsed_sec=30)
        after = lock_position_after_trade(
            before,
            high_up.outcome,
            high_up.size,
            high_up.limit_price,
            high_up.notional,
            high_up.fee,
            high_up.total_cost,
        )

        reason, score = self.bot.score_lock_candidate(
            before,
            after,
            high_up,
            would_lock=False,
            improvement=after["worst_pnl"] - before["worst_pnl"],
            btc_flow={"available": False},
        )

        self.assertEqual(reason, "initial_high_price_wait")
        self.assertEqual(score[0], Decimal("0"))

    def test_initial_probe_uses_deadline_fallback(self) -> None:
        before = position()
        timed_up = decision("Up", "0.51", "0.52", "10.4", elapsed_sec=50)
        after = lock_position_after_trade(
            before,
            timed_up.outcome,
            timed_up.size,
            timed_up.limit_price,
            timed_up.notional,
            timed_up.fee,
            timed_up.total_cost,
        )

        reason, score = self.bot.score_lock_candidate(
            before,
            after,
            timed_up,
            would_lock=False,
            improvement=after["worst_pnl"] - before["worst_pnl"],
            btc_flow={"available": False},
        )

        self.assertEqual(reason, "initial_timed_entry_probe")
        self.assertGreater(score[0], Decimal("0"))

    def test_first_rebalance_can_repair_shallow_locked_loss(self) -> None:
        before = position(
            up_size="20",
            up_cost="10.5",
            up_trade_count="1",
            trade_count="1",
        )
        repair_down = decision("Down", "0.54", "0.54", "10.8")
        after = lock_position_after_trade(
            before,
            repair_down.outcome,
            repair_down.size,
            repair_down.limit_price,
            repair_down.notional,
            repair_down.fee,
            repair_down.total_cost,
        )

        reason, score = self.bot.score_lock_candidate(
            before,
            after,
            repair_down,
            would_lock=False,
            improvement=after["worst_pnl"] - before["worst_pnl"],
            btc_flow={"available": True, "outcome": "Down", "confidence": Decimal("0.4")},
        )

        self.assertEqual(reason, "early_matrix_repair")
        self.assertGreater(score[0], Decimal("0"))
        self.assertGreater(after["worst_pnl"], before["worst_pnl"])


if __name__ == "__main__":
    unittest.main()
