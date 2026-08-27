"""FantasyPros consensus ingest (best-effort scrape of public pages).

Two products:
  * ECR draft rankings  -> data/raw/fantasypros/ecr_draft.parquet
  * Weekly projections  -> data/raw/fantasypros/projections_<pos>.parquet

FantasyPros embeds a JSON blob (``var ecrData = {...}``) in its ranking pages and a
plain HTML table in its projection pages. Both are brittle; every function degrades
to an empty frame + logged warning rather than raising, so a broken selector never
takes down a playbook. When empty, ``projections.py`` falls back to the nflverse
``ff_rankings`` table and the usage model.
"""

from __future__ import annotations

import json
import re

import httpx
import polars as pl
from selectolax.parser import HTMLParser

from .. import store

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 league-engine/0.1"
_ECR_RE = re.compile(r"var\s+ecrData\s*=\s*(\{.*?\});", re.DOTALL)

POSITIONS = ("qb", "rb", "wr", "te", "k", "dst")


def _get(url: str) -> str | None:
    try:
        r = httpx.get(url, headers={"User-Agent": _UA}, timeout=20, follow_redirects=True)
        r.raise_for_status()
        return r.text
    except Exception as exc:  # noqa: BLE001
        print(f"  [fantasypros] fetch failed {url}: {exc}")
        return None


def fetch_ecr_draft(scoring: str = "PPR") -> pl.DataFrame:
    """Consensus draft rankings. scoring in {STD, HALF, PPR}."""
    slug = {"STD": "consensus-cheatsheets", "HALF": "half-point-ppr-cheatsheets",
            "PPR": "ppr-cheatsheets"}.get(scoring.upper(), "ppr-cheatsheets")
    html = _get(f"https://www.fantasypros.com/nfl/rankings/{slug}.php")
    rows: list[dict] = []
    if html:
        m = _ECR_RE.search(html)
        if m:
            try:
                data = json.loads(m.group(1))
                for p in data.get("players", []):
                    rows.append(
                        {
                            "fp_id": p.get("player_id"),
                            "name": p.get("player_name"),
                            "team": p.get("player_team_id"),
                            "position": (p.get("player_position_id") or "").upper(),
                            "bye": p.get("player_bye_week"),
                            "ecr": p.get("rank_ecr"),
                            "ecr_min": p.get("rank_min"),
                            "ecr_max": p.get("rank_max"),
                            "ecr_std": p.get("rank_std"),
                            "adp": p.get("player_owned_avg") or p.get("adp"),
                            "tier": p.get("tier"),
                        }
                    )
            except json.JSONDecodeError as exc:
                print(f"  [fantasypros] ecr JSON parse failed: {exc}")
    df = pl.DataFrame(rows) if rows else pl.DataFrame(
        schema={"fp_id": pl.Utf8, "name": pl.Utf8, "team": pl.Utf8, "position": pl.Utf8,
                "bye": pl.Int64, "ecr": pl.Float64, "ecr_min": pl.Float64, "ecr_max": pl.Float64,
                "ecr_std": pl.Float64, "adp": pl.Float64, "tier": pl.Int64}
    )
    return df


def fetch_projections(pos: str, scoring: str = "PPR", week: int | str = "draft") -> pl.DataFrame:
    """Per-position projection table. week='draft' for season-long, or an int week."""
    url = f"https://www.fantasypros.com/nfl/projections/{pos}.php?scoring={scoring.upper()}"
    if week != "draft":
        url += f"&week={week}"
    html = _get(url)
    if not html:
        return pl.DataFrame()
    tree = HTMLParser(html)
    table = tree.css_first("table#data")
    if table is None:
        print(f"  [fantasypros] no projections table for {pos}")
        return pl.DataFrame()

    headers = [th.text(strip=True).lower() for th in table.css("thead th")]
    rows: list[dict] = []
    for tr in table.css("tbody tr"):
        cells = [td.text(strip=True) for td in tr.css("td")]
        if not cells or len(cells) < 2:
            continue
        name_cell = tr.css_first("td a")
        rec = {"player": name_cell.text(strip=True) if name_cell else cells[0], "position": pos.upper()}
        for h, c in zip(headers[1:], cells[1:], strict=False):
            rec[h] = _num(c)
        rows.append(rec)
    return pl.DataFrame(rows) if rows else pl.DataFrame()


def _num(s: str) -> float | None:
    s = s.replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def sync(scoring: str = "PPR", week: int | str = "draft") -> None:
    layer = "raw/fantasypros"
    store.write(fetch_ecr_draft(scoring), layer, "ecr_draft")
    for pos in POSITIONS:
        df = fetch_projections(pos, scoring, week)
        if df.height:
            store.write(df, layer, f"projections_{pos}")
