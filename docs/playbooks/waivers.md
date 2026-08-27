# Playbook: `waivers`

Rank waiver adds, drop candidates and FAAB bids for the coming week.

```bash
uv run playbook waivers --week 3
```

## Logic (`analysis/waivers.candidates`)

1. **Pool**: players projected for week N, filtered to those rostered in
   `< own_threshold`% of leagues (nflverse weekly ownership when available;
   otherwise the full pool minus your roster).
2. **Baseline**: your current *worst starter* projection at each position
   (computed in `cli.py` from `proj_week_N` ∩ your roster).
3. **`pts_added`** = candidate mean − your positional baseline. Only positive kept.
4. **`faab_bid`** = `(pts_added / max pts_added)^1.5 × 0.6 × season_budget`, rounded.
   Aggressive on the one guy who actually moves your lineup, cheap on depth.

## Output

`name | position | mean | floor | ceiling | pts_added | faab_bid`

- Bid the number for your top 1–2 targets; halve it for speculative stashes.
- Cross-check against `analysis/waivers.trending()` (trending adds) — a name high on
  both is a priority claim.

## Drops

Your roster players with the lowest rest-of-season projection at a position where
you're over roster requirements. (Wired once Yahoo roster snapshots land; until then
eyeball your `my_roster.csv` against `proj_week_N`.)

## Tuning

- `own_threshold` (default 50) — lower it in a sharp league.
- FAAB curve exponent / 0.6 multiplier in `analysis/waivers.candidates`.
