"""Rest-of-season Monte Carlo: playoff odds, projected finish, swing weeks."""

from __future__ import annotations

import numpy as np
import polars as pl


def simulate(
    schedule: list[tuple[int, str, str]],
    team_week_mean: dict[tuple[str, int], float],
    team_week_sd: dict[tuple[str, int], float],
    current_wins: dict[str, int],
    current_pf: dict[str, float],
    playoff_teams: int = 6,
    iterations: int = 10000,
    seed: int = 17,
) -> pl.DataFrame:
    """Simulate remaining head-to-head weeks.

    Args:
        schedule: list of (week, home_team, away_team) still to play.
        team_week_mean/sd: projected weekly score distribution per (team, week).
        current_wins / current_pf: standings so far.

    Returns per-team: playoff_odds, avg_wins, avg_seed, title_odds(approx).
    """
    rng = np.random.default_rng(seed)
    teams = sorted(set(current_wins))
    idx = {t: i for i, t in enumerate(teams)}
    n = len(teams)

    playoff_count = np.zeros(n)
    seed_sum = np.zeros(n)
    wins_sum = np.zeros(n)

    for _ in range(iterations):
        wins = np.array([current_wins[t] for t in teams], dtype=float)
        pf = np.array([current_pf[t] for t in teams], dtype=float)
        for wk, home, away in schedule:
            hm = team_week_mean.get((home, wk), 100.0)
            am = team_week_mean.get((away, wk), 100.0)
            hs = rng.normal(hm, max(team_week_sd.get((home, wk), 25.0), 1.0))
            as_ = rng.normal(am, max(team_week_sd.get((away, wk), 25.0), 1.0))
            pf[idx[home]] += hs
            pf[idx[away]] += as_
            if hs >= as_:
                wins[idx[home]] += 1
            else:
                wins[idx[away]] += 1
        order = sorted(range(n), key=lambda i: (wins[i], pf[i]), reverse=True)
        for rank, i in enumerate(order):
            seed_sum[i] += rank + 1
            if rank < playoff_teams:
                playoff_count[i] += 1
        wins_sum += wins

    return pl.DataFrame(
        {
            "team": teams,
            "playoff_odds": (playoff_count / iterations).round(3),
            "avg_wins": (wins_sum / iterations).round(2),
            "avg_seed": (seed_sum / iterations).round(2),
        }
    ).sort("playoff_odds", descending=True)


def swing_weeks(
    schedule: list[tuple[int, str, str]],
    my_team: str,
    base_odds: float,
    sim_fn,
) -> pl.DataFrame:
    """For each of my remaining games, the playoff-odds delta between a forced win and loss."""
    rows = []
    for wk, home, away in schedule:
        if my_team not in (home, away):
            continue
        win_odds = sim_fn(force={(wk, my_team): "W"})
        loss_odds = sim_fn(force={(wk, my_team): "L"})
        rows.append(
            {
                "week": wk,
                "opponent": away if home == my_team else home,
                "odds_if_win": round(win_odds, 3),
                "odds_if_loss": round(loss_odds, 3),
                "swing": round(win_odds - loss_odds, 3),
            }
        )
    return pl.DataFrame(rows).sort("swing", descending=True) if rows else pl.DataFrame()
