# Playbook: `lineup`

Set the optimal starting lineup for a given week.

```bash
uv run playbook lineup --week 3
```

## Inputs

- `derived/proj_week_<week>.parquet` — built automatically by this command
  (`projections.weekly_projection`): blend of recent league PPG (last 6 games),
  season PPG, and the preseason projection as a prior for players with <2 games.
  `sd` = recent std (fallback 40% of mean); `floor`/`ceiling` at p20/p80.
- **Your roster** — from `config/my_roster.csv` (`name,position[,out]`) until Yahoo
  roster endpoints are wired into `sync`. Set `out=true` for bye/injured/suspended.

## Logic (`analysis/lineup.optimize`)

Integer LP (PuLP/CBC): maximise summed projected points subject to exact roster-slot
constraints, flex eligibility (`W/R/T`, `Q/W/R/T`, etc.), one slot per player, no
locked-out players. `objective` can be `proj` (default), `floor`, or `ceiling`.

## Output

`slot | starter | proj | floor | ceiling | next_best | gap`

- `gap` < ~2 pts = a real coin-flip; check `floor`/`ceiling` and matchup before locking.
- Run with `objective="ceiling"` in a week you're a big underdog, `"floor"` when
  you're favoured — compare against the `proj` lineup for the swaps it implies.

## Verify

On a past week, feed realised points instead of projections: the LP must return the
literal highest-scoring legal lineup (optimality check in `tests/`).
