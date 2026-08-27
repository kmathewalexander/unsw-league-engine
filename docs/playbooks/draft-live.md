# Playbook: `draft-live`

Interactive assistant for draft day.

```bash
uv run playbook draft-live --teams 12 --picks-until-next 12
```

`--picks-until-next` is how many picks happen before your next turn (in a 12-team
snake, that's ~12–22 depending on where you sit; update it as the draft moves).

## Commands at the `draft>` prompt

| Input | Meaning |
|---|---|
| `Justin Jefferson` | someone drafted this player |
| `me Bijan Robinson` | **you** drafted this player |
| *(blank line)* | print recommendations for your pick now |
| `q` | quit |

## How recommendations are scored (`analysis/draft.recommend_pick`)

```
pick_score = VOR
           + need_bonus                     # thin at RB/WR (<2) or no QB/TE yet
           + max(VOR,0) * P(gone by next pick) * 0.4   # urgency: good AND about to vanish
```

`P(gone by next pick)` is a logistic on `(my_next_pick_number − ADP)`, so the engine
tells you to grab a faller now rather than hope they survive the turn.

Negative `need_bonus` on K/DEF suppresses them until your roster is nearly full.

**Read it as:** take the top `pick_score`, unless two names are within ~5 points and
one is the last of his tier (`tier` column) — then take the tier-breaker.

## During the draft

- Keep `--picks-until-next` roughly right; it drives the urgency term.
- After ~each round, the board doesn't need rebuilding — it's static ranks vs a live
  "taken" set.
- If you disagree with a `season_points` value, it's almost always the rank→points
  curve or a stale prior year; note it and adjust post-draft.
