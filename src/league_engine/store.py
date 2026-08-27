"""Local data store: Parquet files under ``data/`` with a thin DuckDB query layer.

Layout::

    data/
      raw/yahoo/<season>/<dataset>.parquet      # snapshots straight from Yahoo
      raw/nflverse/<dataset>.parquet            # nflverse pulls
      raw/fantasypros/<dataset>.parquet
      derived/<dataset>.parquet                 # engine-computed tables
      meta.json                                 # last-sync timestamps per dataset

Everything is Polars in / Polars out. ``query()`` is available for ad-hoc SQL across files.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import polars as pl

from .config import load_settings


def _base() -> Path:
    return load_settings().data_dir


def path_for(layer: str, *parts: str) -> Path:
    """Build a path under data/<layer>/<parts...>.parquet (parts without extension)."""
    p = _base() / layer
    for part in parts[:-1]:
        p = p / str(part)
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{parts[-1]}.parquet"


def write(df: pl.DataFrame, layer: str, *parts: str) -> Path:
    target = path_for(layer, *parts)
    df.write_parquet(target)
    _touch_meta("/".join([layer, *map(str, parts)]), rows=df.height)
    return target


def read(layer: str, *parts: str) -> pl.DataFrame:
    target = path_for(layer, *parts)
    if not target.exists():
        raise FileNotFoundError(f"No snapshot at {target}. Run `playbook sync` first.")
    return pl.read_parquet(target)


def exists(layer: str, *parts: str) -> bool:
    return path_for(layer, *parts).exists()


def json_path_for(layer: str, *parts: str) -> Path:
    p = _base() / layer
    for part in parts[:-1]:
        p = p / str(part)
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{parts[-1]}.json"


def write_json(obj: object, layer: str, *parts: str) -> Path:
    """Store a raw (possibly deeply nested) payload as JSON, e.g. Yahoo model dumps."""
    target = json_path_for(layer, *parts)
    target.write_text(json.dumps(obj, indent=2, default=str))
    n = len(obj) if isinstance(obj, (list, dict)) else 1
    _touch_meta("/".join([layer, *map(str, parts), "json"]), rows=n)
    return target


def read_json(layer: str, *parts: str) -> object:
    target = json_path_for(layer, *parts)
    if not target.exists():
        raise FileNotFoundError(f"No snapshot at {target}. Run `playbook sync` first.")
    return json.loads(target.read_text())


def json_exists(layer: str, *parts: str) -> bool:
    return json_path_for(layer, *parts).exists()


def query(sql: str) -> pl.DataFrame:
    """Run DuckDB SQL. Reference files by absolute path, e.g.::

        SELECT * FROM read_parquet('.../data/raw/nflverse/player_stats.parquet')
    """
    con = duckdb.connect()
    try:
        return con.execute(sql).pl()
    finally:
        con.close()


def _meta_path() -> Path:
    return _base() / "meta.json"


def _touch_meta(dataset: str, rows: int) -> None:
    meta = load_meta()
    meta[dataset] = {"synced_at": datetime.now(UTC).isoformat(), "rows": rows}
    _meta_path().write_text(json.dumps(meta, indent=2, sort_keys=True))


def load_meta() -> dict:
    if _meta_path().exists():
        return json.loads(_meta_path().read_text())
    return {}
