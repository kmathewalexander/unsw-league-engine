---
name: league-trend
description: Run the weekly trend analysis for the UNSW league — scoring, form, scheme fit and schedule difficulty per player and per fantasy team, with week-over-week movement. Use to answer who is rising/falling, buy/sell calls, or how a roster is trending.
---

# league-trend

```bash
uv run playbook trend            # current upcoming week; --week N to pin
```

Saves `data/derived/trend/week_N.parquet` and `reports/trend-weekN-*.md`.

## Components (z-scores within position; weights 0.35 / 0.25 / 0.20 / 0.20)

- **scoring** — shrunk forecast PPG in league points (`analysis/review.py`).
- **form** — last-2 vs baseline, minus finishing luck (actual − xFP), plus snap-share trend.
- **scheme fit** — team pass/rush lean over the last 3 games (RB likes run-heavy, QB/WR/TE
  pass-heavy) blended with the player's volume-share change vs last season (role change).
- **schedule** — next 4 opponents' points allowed to the position (2025 + 2026, 2026 weighted 2x).

`trend_delta` / `rank_delta` compare with the previous week's snapshot (blank in the first week).
`drivers` names the two components moving the score most.

## How to read it

- High `trend_score` with `SELL` signal = production outrunning usage; do not chase.
- Negative `z_form` with `BUY` signal = usage is there, results will follow — hold/buy.
- Use `z_schedule` as a tiebreaker for start/sit and waiver timing, not as the main call.
- Small samples (weeks 1-3): trust `z_scoring` (prior-heavy) over `z_form`.

Report my roster's risers/fallers first, then free-agent risers, then the team table.
