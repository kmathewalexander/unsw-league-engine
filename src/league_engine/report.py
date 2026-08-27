"""Report rendering: Markdown files to reports/ + a rich summary to the terminal."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import polars as pl
from rich.console import Console
from rich.table import Table

from .config import load_settings

console = Console()


def df_to_md(df: pl.DataFrame, max_rows: int = 50) -> str:
    if df.is_empty():
        return "_(no rows)_\n"
    df = df.head(max_rows)
    cols = df.columns
    lines = ["| " + " | ".join(cols) + " |", "| " + " | ".join("---" for _ in cols) + " |"]
    for row in df.iter_rows():
        lines.append("| " + " | ".join(_fmt(v) for v in row) + " |")
    return "\n".join(lines) + "\n"


def _fmt(v) -> str:
    if isinstance(v, float):
        return f"{v:.2f}"
    return "" if v is None else str(v)


def print_df(df: pl.DataFrame, title: str, max_rows: int = 25) -> None:
    t = Table(title=title, header_style="bold cyan")
    for c in df.columns:
        t.add_column(c)
    for row in df.head(max_rows).iter_rows():
        t.add_row(*[_fmt(v) for v in row])
    console.print(t)


def save(name: str, sections: list[tuple[str, str]]) -> Path:
    """Write a timestamped markdown report. sections = [(heading, body_md), ...]."""
    settings = load_settings()
    ts = datetime.now().strftime("%Y%m%d-%H%M")
    path = settings.reports_dir / f"{name}-{ts}.md"
    parts = [f"# {name} — {ts}", ""]
    for heading, body in sections:
        parts += [f"## {heading}", "", body, ""]
    path.write_text("\n".join(parts))
    console.print(f"[green]report written[/green] {path}")
    return path
