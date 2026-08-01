"""Export stage: foobar2000 metadb.sqlite -> intermediate JSON.

Read-only against the foobar database. Produces records keyed by the
album-relative path tail, aggregating any duplicate copies of the same song.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass

from .core import STATS_INDEX_GUID, Stats, parse_stats_blob, path_tail

SCHEMA_VERSION = 1


@dataclass
class Record:
    tail: str
    play_count: int
    first_played_ms: int | None
    last_played_ms: int | None
    added_ms: int | None
    rating_star: int | None


def _merge_dup(a: Record, s: Stats) -> None:
    """Fold a second foobar entry that maps to the same tail into ``a``.

    Duplicate physical copies of one song collapse to a single fooyin
    TrackHash, so we combine them the same way import merges: max play count,
    earliest first/added, latest last, strongest rating.
    """
    a.play_count = max(a.play_count, s.play_count)
    a.first_played_ms = _min_opt(a.first_played_ms, s.first_played_ms)
    a.last_played_ms = _max_opt(a.last_played_ms, s.last_played_ms)
    a.added_ms = _min_opt(a.added_ms, s.added_ms)
    a.rating_star = _max_opt(a.rating_star, s.rating_star)


def _min_opt(x: int | None, y: int | None) -> int | None:
    vals = [v for v in (x, y) if v is not None]
    return min(vals) if vals else None


def _max_opt(x: int | None, y: int | None) -> int | None:
    vals = [v for v in (x, y) if v is not None]
    return max(vals) if vals else None


def export(foobar_db: str) -> dict:
    """Read foobar stats and return the JSON-serializable payload."""
    uri = f"file:{foobar_db}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        g = STATS_INDEX_GUID
        rows = conn.execute(
            f"SELECT n.filename, d.value "
            f"FROM metadb_index_{g} n "
            f"JOIN metadb_index_{g}_data d ON n.key = d.key "
            f"WHERE n.filename LIKE '%file://%'"
        )
        by_tail: dict[str, Record] = {}
        skipped_no_tail = 0
        skipped_bad_blob = 0
        for filename, blob in rows:
            tail = path_tail(filename)
            if tail is None:
                skipped_no_tail += 1
                continue
            stats = parse_stats_blob(blob)
            if stats is None:
                skipped_bad_blob += 1
                continue
            existing = by_tail.get(tail)
            if existing is None:
                by_tail[tail] = Record(tail=tail, **_stats_fields(stats))
            else:
                _merge_dup(existing, stats)
    finally:
        conn.close()

    records = [asdict(r) for r in by_tail.values()]
    return {
        "version": SCHEMA_VERSION,
        "source": foobar_db,
        "count": len(records),
        "skipped_no_tail": skipped_no_tail,
        "skipped_bad_blob": skipped_bad_blob,
        "records": records,
    }


def _stats_fields(s: Stats) -> dict:
    return {
        "play_count": s.play_count,
        "first_played_ms": s.first_played_ms,
        "last_played_ms": s.last_played_ms,
        "added_ms": s.added_ms,
        "rating_star": s.rating_star,
    }


def write_json(payload: dict, out_path: str) -> None:
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
