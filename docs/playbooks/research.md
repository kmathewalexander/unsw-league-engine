# Research layer

`src/league_engine/research.py` builds `data/derived/research.parquet` — per-player
context that joins onto the value board (`research.enriched()`). Rebuilt by `sync`,
`draft-prep`, and `draft-board`.

## Data points

| Column | Source | Meaning |
|---|---|---|
| `flag` | `config/injuries.csv` → `players.status` | Injury / PUP / IR / suspension note. **Refresh `injuries.csv` day-of.** |
| `usage_grade` | `snap_counts` + `ff_opportunity` | A–F, percentile of snap% + expected points within position (prior season) |
| `xfp_pg` | `ff_opportunity` | Expected fantasy points / game — volume-based, strips TD luck |
| `fp_oe_pg` | `ff_opportunity` | Actual − expected / game. Big + with low `xfp_pg` = unsustainable (TD regression). Big + with high `xfp_pg` = just elite. |
| `team_total` | `config/team_context.toml` win totals | Implied points/game ≈ 22.5 + (win_total − 8.5)×1.1. Stable, all 32 teams. |
| `team_total_early` | 2026 schedule lines (wks ~1–4) | Directional early-season read; noisy for teams with few games priced |
| `win_total` | `config/team_context.toml` | 2026 Vegas season win total |
| `sos_season` / `sos_playoffs` | 2026 schedule × 2025 points-allowed-by-position | z-score, **higher = softer**. Playoffs = NFL weeks 16–17 (this league's fantasy playoffs) |
| `age`, `rookie`, `draft_round`, `draft_pick` | `players` | RB age ≥ 28 flagged; rookie flag; draft capital for players with no NFL usage |

## Keeping it current

- **`config/injuries.csv`** — the one thing that goes stale fast. Columns `name,injury`.
  Update the morning of the draft from a training-camp injury report. `_nkey`
  normalisation matches names loosely (suffixes, punctuation).
- **`config/team_context.toml`** — win totals. Update if lines move meaningfully.
- Everything else regenerates from nflverse on `sync`.

## Not included / why

- **PlayerProfiler** — rankings are paywalled; player-page metrics render via JS and
  don't scrape cleanly. The signature metrics (opportunity share, snap share, target
  share, athletic scores, breakout age) are replicated above from nflverse. For PP's
  specific take on one player (Best Comparable Player, their blurb), pull the page live.
- **Team DEF scoring** — still not in manual mode; DEF picks show 0.
