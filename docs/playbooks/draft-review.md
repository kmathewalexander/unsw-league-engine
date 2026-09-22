# Playbook: `draft-review`

Draft prep for next year: how each manager drafts and who appears to do their research.

```bash
uv run playbook draft-review
```

## Data
- `config/draft_history/2025.txt`, `2026.txt` — Yahoo draft results (`Player|POS`, pick order).
- `config/draft_history/orders.toml` — round-1 order per season (snake) + franchise renames
  (PatBot→Team Autopick and Dad Bodgers→Make Draye both confirmed by Yahoo's `/lastseason` page).
- nflverse archive `raw/nflverse/ff_rankings_draft_history` (FantasyPros ECR snapshots, Jul-Sep).
  Market price = latest superflex `redraft-op` snapshot on/before draft day (2026-08-29; 2025 draft
  day is assumed to be 2025-09-03 — edit `_CUTOFF` in `analysis/draft_review.py`).
- `derived/league_points` — 2025 outcomes in this league's scoring.

## Signals
| Signal | Meaning |
|---|---|
| `consensus_gap` | mean abs (reach − league habit for that position). Low = follows the market |
| `deviation_edge` | payoff of picks >=15 slots off-market minus payoff of consensus picks (>=3 needed) |
| `hit_rate`, `bust_rate` | finished at/above the pick slot; >60 places worse (rounds 1-8) |
| `surplus_vs_mkt` | places gained vs market price, relative to league average |
| `research_signal` | z-sum of deviation_edge, hit_rate, surplus_vs_mkt — a proxy, not a verdict |
| `moves` | in-season waiver/trade moves (standings.csv) |
| tendencies | first QB/TE round, QB in R1-3, RB/WR in R1-5, K/DEF round, rookie share, homer stacks |

League-wide: everyone drafts QBs ~11 picks later than the superflex market and RBs ~12 earlier
(`league_position_reach`), so a manager's `idio` is measured against that norm.

## Caveats
Only two seasons; ECR archive is weekly-ish, not draft-minute; outcomes ignore injuries/keepers;
K/DEF are excluded from reach. Treat as leads to test next draft.
