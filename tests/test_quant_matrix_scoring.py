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


def decision(outcome: str, probability: str, price: str, total_cost: str) -> QuantDecision:
    market = QuantMarket(
        slug="btc-updown-5m-test",
        title="test market",
        start_ts=int(time.time()) - 120,
        end_ts=int(time.time()) + 180,
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


if __name__ == "__main__":
    unittest.main()
