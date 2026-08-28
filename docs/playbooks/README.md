# Playbooks

Each playbook is a repeatable command (`uv run playbook <name>`) plus a documented
decision procedure. Run `playbook sync` first each day you use the engine.

| Playbook | Doc | When to run |
|---|---|---|
| `sync` | [sync.md](sync.md) | Start of every session |
| `draft-prep` | [draft-prep.md](draft-prep.md) | Days before the draft |
| `draft-board` | [draft-board.md](draft-board.md) | During the draft — paste Yahoo's pick list |
| `draft-live` | [draft-live.md](draft-live.md) | During the draft — type picks manually (fallback) |
| `lineup` | [lineup.md](lineup.md) | Wed–Sun each game week |
| `waivers` | [waivers.md](waivers.md) | Tue (after MNF), before waivers process |
| `trades` | [trades.md](trades.md) | Weekly, or when you have a clear surplus/need |
| `standings` | [standings.md](standings.md) | Weekly from ~Week 6 |
| `brief` | — | Combines lineup + waivers + standings |

## How the numbers are built

```
Yahoo settings ──► scoring.py ──► score every nflverse player-week in LEAGUE points
                                        │
nflverse stats ─────────────────────────┤──► derived/league_points.parquet
                                        │
nflverse ff_rankings (ECR) ─────────────┼──► projections.py ──► proj_season / proj_week_N
prior-year league points ───────────────┘                            │
                                                                     ▼
                                     analysis/{draft,lineup,waivers,trades,standings}.py
```

Everything is expressed in **your league's scoring**, not generic PPR. If
`playbook scoring-check` lists any "UNMAPPED" stats, add an expression for them in
`src/league_engine/scoring.py:STAT_EXPR` before trusting the outputs.
