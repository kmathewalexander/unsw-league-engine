# Playbooks: `track`, `trend`, `weekly`

Weekly state tracking + trend analysis. Full method and reading guide live in the skills
(`.claude/skills/league-track`, `league-trend`, `league-waivers`, `league-weekly`); the
background runner is the `league-analyst` agent (`.claude/agents/league-analyst.md`).

```bash
uv run playbook track            # snapshot rosters, scoring, waiver wire, standings -> data/raw/league
uv run playbook trend            # scoring x form x scheme x schedule -> data/derived/trend
uv run playbook weekly           # sync -> track -> trend -> review -> waivers
```

## Data (same store as everything else: Parquet under `data/`, one file per week)

```
data/raw/league/{rosters,standings,scoring,waivers,transactions}/week_<N>.parquet
data/derived/league/{roster_moves,scoring_changes,waiver_moves}.parquet   # week-over-week diffs
data/derived/trend/week_<N>.parquet                                       # trend scores + deltas
```

Yahoo's API is gated, so `config/league_rosters.csv`, `config/standings.csv` (and optional
`transactions.csv`, `schedule.csv`) are refreshed from the Yahoo pages by the agent/skill and
then snapshotted. Those files are gitignored (`.example` files show the format).
