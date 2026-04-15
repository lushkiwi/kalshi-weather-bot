from __future__ import annotations

from kalshi_weather_bot.edge.detector import (
    Candidate,
    best_candidate,
    effective_edge_min,
    evaluate,
)
from kalshi_weather_bot.edge.implied import MarketImplied


def _imp(yes_bid: int | None, yes_ask: int | None) -> MarketImplied:
    mid = None
    if yes_bid is not None and yes_ask is not None:
        mid = (yes_bid + yes_ask) / 200.0
    return MarketImplied(ticker="T", yes_bid=yes_bid, yes_ask=yes_ask, mid=mid)


def test_effective_edge_min_far_from_close():
    # h >= decay_hours → no ramp.
    assert effective_edge_min(0.04, hours_to_close=24.0, decay_hours=6.0) == 0.04
    assert effective_edge_min(0.04, hours_to_close=6.0, decay_hours=6.0) == 0.04


def test_effective_edge_min_at_close_doubles():
    assert effective_edge_min(0.04, hours_to_close=0.0, decay_hours=6.0) == 0.08


def test_effective_edge_min_halfway_ramp():
    # h=3, decay=6 → 1.5x.
    assert effective_edge_min(0.04, hours_to_close=3.0, decay_hours=6.0) == 0.06


def test_effective_edge_min_zero_decay_disables_ramp():
    assert effective_edge_min(0.04, hours_to_close=0.0, decay_hours=0.0) == 0.04


def test_evaluate_returns_two_candidates_when_book_full():
    cands = evaluate(
        p_fair=0.55,
        implied=_imp(40, 46),
        edge_min=0.04,
        hours_to_close=24.0,
        decay_hours=6.0,
    )
    sides = {c.side for c in cands}
    assert sides == {"buy_yes", "buy_no"}


def test_evaluate_buy_yes_flagged_when_fair_beats_ask():
    # yes_ask=46 → cost 0.46, fee(0.46)=ceil(1.75*0.46*0.54*... no — taker C=1
    # at P=0.46 → ceil(0.07 * 0.46 * 0.54 * 100) = ceil(1.7388) = 2 cents = 0.02.
    # p_fair=0.55 → gross=0.09, net=0.07, threshold=0.04 → flagged.
    cands = evaluate(
        p_fair=0.55,
        implied=_imp(40, 46),
        edge_min=0.04,
        hours_to_close=24.0,
        decay_hours=6.0,
    )
    buy_yes = next(c for c in cands if c.side == "buy_yes")
    assert buy_yes.p_market_cost == 0.46
    assert abs(buy_yes.gross_edge - 0.09) < 1e-9
    assert buy_yes.fee_rate == 0.02
    assert abs(buy_yes.net_edge - 0.07) < 1e-9
    assert buy_yes.flagged


def test_evaluate_buy_no_flagged_when_fair_low():
    # p_fair=0.35, yes_bid=40 → buy NO cost = 0.60, p_win = 0.65.
    # gross = 0.05, fee(0.60) = ceil(0.07*0.60*0.40*100)=ceil(1.68)=2 → 0.02.
    # net = 0.03. Below 0.04 threshold → not flagged.
    cands = evaluate(
        p_fair=0.35,
        implied=_imp(40, 46),
        edge_min=0.04,
        hours_to_close=24.0,
        decay_hours=6.0,
    )
    buy_no = next(c for c in cands if c.side == "buy_no")
    assert abs(buy_no.net_edge - 0.03) < 1e-9
    assert not buy_no.flagged


def test_time_decay_tightens_flag_near_close():
    # Same p_fair=0.52, ask=46 → net edge = 0.06 - 0.02 = 0.04.
    # At h=24 threshold=0.04 → net > threshold is False (0.04 > 0.04 is False).
    # At h=0 threshold=0.08 → also not flagged.
    cands_far = evaluate(
        p_fair=0.52,
        implied=_imp(40, 46),
        edge_min=0.04,
        hours_to_close=24.0,
        decay_hours=6.0,
    )
    cands_near = evaluate(
        p_fair=0.52,
        implied=_imp(40, 46),
        edge_min=0.04,
        hours_to_close=0.0,
        decay_hours=6.0,
    )
    buy_yes_far = next(c for c in cands_far if c.side == "buy_yes")
    buy_yes_near = next(c for c in cands_near if c.side == "buy_yes")
    assert buy_yes_far.effective_edge_min == 0.04
    assert buy_yes_near.effective_edge_min == 0.08
    # Same net edge, threshold doubled.
    assert buy_yes_far.net_edge == buy_yes_near.net_edge
    assert not buy_yes_near.flagged


def test_evaluate_skips_side_when_quote_missing():
    cands = evaluate(
        p_fair=0.55,
        implied=_imp(None, 46),
        edge_min=0.04,
        hours_to_close=24.0,
        decay_hours=6.0,
    )
    sides = {c.side for c in cands}
    assert sides == {"buy_yes"}


def test_best_candidate_picks_max_net_edge():
    cands = [
        Candidate("T", "buy_yes", 0.55, 0.46, 0.09, 0.02, 0.07, 0.04, True),
        Candidate("T", "buy_no", 0.45, 0.60, -0.15, 0.02, -0.17, 0.04, False),
    ]
    best = best_candidate(cands)
    assert best is not None
    assert best.side == "buy_yes"


def test_best_candidate_none_flagged():
    cands = [
        Candidate("T", "buy_yes", 0.55, 0.46, 0.02, 0.02, 0.00, 0.04, False),
    ]
    assert best_candidate(cands) is None
