---
name: league-track
description: Refresh and snapshot the UNSW league state — all 12 rosters, league scoring rules, the waiver wire and standings — from Yahoo into the repo's data store. Use at the start of each week or when asked what changed in the league.
---

# league-track

Tracks four things, one snapshot per week, in the same Parquet store as the rest of the engine:

| Thing | Staging input | Stored at |
|---|---|---|
| Rosters (all teams) | `config/league_rosters.csv` | `data/raw/league/rosters/week_N` (+ `derived/league/roster_moves`) |
| Scoring rules | `config/scoring.toml` | `data/raw/league/scoring/week_N` (+ `derived/league/scoring_changes`) |
| Waiver wire | computed: nflverse pool minus rostered | `data/raw/league/waivers/week_N` (+ `derived/league/waiver_moves`) |
| Standings | `config/standings.csv` | `data/raw/league/standings/week_N` |

## 1. Refresh staging files from Yahoo (Chrome MCP, read-only)

League `https://football.fantasysports.yahoo.com/f1/827662`; team pages are `/f1/827662/<1-12>`
(mine is 8, "Nifty Knights Ni!"). Load the Chrome tools, open a tab, then in `javascript_tool`
`fetch('/f1/827662/<id>')`, parse with `DOMParser`, and read `table tbody tr` rows whose first cell
is a slot (QB, RB, WR, TE, W/R/T, Q/W/R/T, K, DEF, BN, IR). Player name = text before
`<TEAM> - <POS>` in the row.

Gotchas learned the hard way:
- Tool output truncates near ~1.2k chars and blocks some long single-line strings — return
  newline-separated `slot:name` lines and fetch 2-3 teams per call.
- Your own team page (8) renders extra dropdown text; parse it from the live page instead of fetch.
- Scoring/rules: the commissioner log on the league home page lists rule changes
  (e.g. pass TD 6→4, INT −3→−2 on Aug 21). Update `config/scoring.toml` when it changes.
- Standings + weekly scores: league home page (`/f1/827662`). `standings.csv` columns:
  `team,wins,losses,pf,pa,w1,w2,...` (one `wN` column per completed week).
- Optional: `config/transactions.csv` (recent adds/drops/trades) and `config/schedule.csv`
  (`week,home,away`) — the latter makes playoff odds exact.

Column format: `config/league_rosters.csv` = `team,slot,name` (see `.example`).

## 2. Snapshot

```bash
uv run playbook sync --skip-yahoo --skip-fantasypros   # refresh nflverse through the latest week
uv run playbook track                                  # add --week N to relabel
```

Then read `data/derived/league/*.parquet` (`store.read("derived/league", "roster_moves")`) and
summarise adds/drops, any scoring change, and players newly available on the wire.
