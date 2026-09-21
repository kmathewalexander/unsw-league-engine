# Playbook: `review` / `player` / `waivers`

Evidence-based analysis in **this league's scoring** (no PPR, 4-pt pass TD, superflex slot).

```bash
uv run playbook review --week 3        # power rankings, all-play luck, playoff odds, my lineup
uv run playbook player "Denzel Boston" # usage / luck / trend profile + marginal gain for my lineup
uv run playbook waivers --week 3       # free agents ranked by marginal optimal-lineup gain
```

## Inputs (snapshot from Yahoo — the API is gated, so refresh these by hand each week)

| File | Contents |
|---|---|
| `config/league_rosters.csv` | `team,slot,name` for all 12 rosters (also defines "rostered" for waivers) |
| `config/standings.csv` | `team,wins,losses,pf,pa,w1,w2,...` (weekly scores drive the all-play check) |
| `config/schedule.csv` (optional) | `week,home,away`; without it odds use a random round-robin (approximation) |

## Method (`analysis/review.py`)

1. **Expected points (xFP)** — nflverse opportunity components (expected yards / TDs / INTs)
   scored with league rules. `finish_luck = actual - xFP` per game: large positive = TD luck
   (SELL / don't chase), large negative = usage ahead of results (BUY).
2. **Shrunk forecast** — `(n*evidence + k*prior)/(n+k)`; evidence = 0.6 xFP + 0.4 actual PPG,
   prior = blend of preseason market PPG and 2025 PPG, `k` = 4-8 pseudo-games by position.
   (The old model used 80% of the last-6-game mean, so one 34-pt game became the forecast.)
3. **Trend** — snap % last game vs 2026 average; usage change vs 2025 xFP.
4. **Volatility** — 2025 weekly SD, boom (>=15) and bust (<=6) rates → floor/ceiling.
5. **Team strength** — ILP-optimal lineup (incl. `Q/W/R/T`) from forecasts; sd = root-sum-square.
6. **Luck check** — all-play win % (each week's score vs every team) next to the real record.
7. **Odds** — Monte Carlo over remaining weeks, top 6 make playoffs.
8. **Waivers** — for each free agent, re-solve *my* optimal lineup with them added; the
   difference is `lineup_gain` (0 = would not start). `replaces` names who they displace.

## Known gaps

- DEF has no scoring model (flat 7.0 placeholder); K uses league PPG.
- No opponent-defence / Vegas adjustment in the weekly forecast yet (`research.py` has SOS + team totals).
- Rookies with no NFL history (e.g. Fernando Mendoza) have no forecast until a game is played.
- Schedule is approximated unless `config/schedule.csv` is supplied.
