"""Lineup ILP: fed realised points, it must return the literal best legal lineup."""

from __future__ import annotations

from league_engine.analysis import lineup as lu
from league_engine.scoring import RosterSlot, ScoringRules

RULES = ScoringRules(
    season=2026,
    rules=[],
    roster=[
        RosterSlot("QB", 1),
        RosterSlot("RB", 2),
        RosterSlot("WR", 2),
        RosterSlot("TE", 1),
        RosterSlot("W/R/T", 1),
        RosterSlot("BN", 5),
    ],
)


def test_optimizer_picks_the_best_legal_lineup():
    players = [
        lu.Player("QB1", "QB", 25), lu.Player("QB2", "QB", 18),
        lu.Player("RB1", "RB", 22), lu.Player("RB2", "RB", 15), lu.Player("RB3", "RB", 14),
        lu.Player("WR1", "WR", 20), lu.Player("WR2", "WR", 17), lu.Player("WR3", "WR", 16),
        lu.Player("TE1", "TE", 12), lu.Player("TE2", "TE", 6),
        lu.Player("RBhurt", "RB", 99, locked_out=True),
    ]
    chosen = lu.optimize(players, RULES, "proj")
    names = {p.name for p in chosen.values()}

    # QB1; RB1+RB2; WR1+WR2; TE1; flex = best remaining of RB3/WR3 => WR3 (16 > 14)
    assert names == {"QB1", "RB1", "RB2", "WR1", "WR2", "TE1", "WR3"}
    assert "RBhurt" not in names
    assert round(sum(p.proj for p in chosen.values()), 1) == 25 + 22 + 15 + 20 + 17 + 12 + 16
