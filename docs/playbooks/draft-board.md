# Playbook: `draft-board`

Paste the Yahoo draft room's pick list, get your next-pick recommendation. This is
the recommended live-draft workflow (more reliable than typing picks one by one).

```bash
uv run playbook draft-board --board board.txt --teams 13
uv run playbook draft-board --board board.txt --brief   # compact one-line-per-candidate output
```

## During the draft

1. In the Yahoo draft room, open the **Draft Results** panel (the running list of picks).
2. Select all of it and copy (Cmd-A, Cmd-C inside the panel — or just drag-select the list).
3. Paste into `board.txt` (overwrite it each time), save.
4. Run the command. Re-run after every few picks.

The paste can be in any order and include round separators / injury tags — the
parser handles Yahoo's exact format:

```
8
D. Maye
D. Maye
QB
NE
Bye 11
Your Team
```

## What it does

- **Parses** every pick: number, player, position, team, and whether it's yours
  (`Your Team` / `My Team`).
- **Resolves** each abbreviated name (`J. Cook III`) to the value board, tolerating
  suffixes and compound surnames (`A. St. Brown`). Anything it can't match is listed
  and skipped — check that list; a wrong skip means a drafted player still shows as
  available.
- **Computes your snake position** from your existing picks and figures out how many
  picks until your *next* turn (drives the urgency term).
- **Recommends** via `recommend_pick`: `VOR + roster-need bonus + urgency`, where
  urgency = `VOR x P(gone before your next pick) x 0.4`.

## Reading the output

`name | position | season_points | vor | adp | tier | need_bonus | p_gone_by_next | pick_score`

- Sort order is `pick_score`. Take the top unless it's a coin-flip with a tier-breaker.
- **`p_gone_by_next` near 1.0** = last chance for that player — if you want him, now.
- **`need_bonus`** shows what the model thinks you're short on (12 = need a 2nd QB in
  this superflex league, 10 = thin at RB/WR, negative = stop drafting K/DEF).
- **`adp`** is standard 1-QB consensus; in this superflex league real QB demand is
  much higher, so treat a QB's ADP as a floor on when he'll actually go.

## `--brief`

Skips the rich table and prints a compact block: your pick number, roster counts,
picks-until-next, then the top 8 candidates as `name | pos | VOR | ADP | tier | notes`
on one line each. Same ranking, far less to scan — use this for fast live picks.

## Override

Pass `--until N` to set picks-until-your-next-turn manually if the auto snake math
is off (e.g. after a traded pick or if the parse missed one of your picks).
