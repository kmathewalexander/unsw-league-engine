---
name: league-weekly
description: Run the full weekly UNSW league cycle end to end — refresh Yahoo, track rosters/scoring/waivers/standings, trend analysis, lineup and waiver recommendations. Use when asked for the weekly update or league brief.
---

# league-weekly

Delegate to the `league-analyst` agent (runs in the background), or run inline:

1. `league-track` skill → refresh `config/*.csv` from Yahoo, then `uv run playbook track`
2. `league-trend` skill → `uv run playbook trend`
3. `league-waivers` skill → `uv run playbook review --week N` and `waivers --week N`

`uv run playbook weekly` chains sync → track → trend → review → waivers once the staging CSVs
are fresh. Summarise decisions first, evidence second, caveats last.
