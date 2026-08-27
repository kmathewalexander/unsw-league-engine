# Playbook: `trades`

Find trades that improve your starting lineup **and** the counterparty's.

```bash
uv run playbook trades
```

> Requires league-wide rosters. Wired once `sync` snapshots every team's roster from
> Yahoo (`get_team_roster_by_week`). Until then, populate rosters manually and call
> `analysis.trades.find_trades()` from a script.

## Logic (`analysis/trades.find_trades`)

1. **Starter strength** = sum of the top-N rest-of-season projections that fill the
   starting slots (flex-aware).
2. Enumerate 1-for-1 and 2-for-2 packages with every other team.
3. Keep only trades where **both** `my_gain > 0.5` and `their_gain > 0.5` ROS points.
4. Rank by `my_gain`, then `fairness` = `min(gain) / max(gain)`.

## Output

`owner | i_give | i_get | my_gain | their_gain | fairness | total_gain`

- `fairness` near 1.0 = an easy sell. Below ~0.4 = you're fleecing them; expect a no.
- `my_gain` is the ROS starting-lineup points you add — a full win is ~15–25 pts, so
  a 30-pt gain is a genuine needle-mover.

## Rest-of-season projections

ROS points per player = remaining games × `proj_season` PPG, adjusted by current-season
form once enough weeks exist. Trade values shift week to week — re-run after injuries.
