"""Export stage: foobar2000 metadb.sqlite -> intermediate JSON.

Read-only against the foobar database. Emits one record per foobar stats entry,
each carrying the recomputed fooyin TrackHash (primary, path-independent match
key) and the album-relative path tail (fallback), so the import stage can match
by content identity and fall back to path where identity can't be reproduced.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass

from .core import (
    STATS_INDEX_GUID,
    Stats,
    fooyin_track_hash,
    parse_info_tags,
    parse_stats_blob,
    path_tail,
    subsong_from_name,
)

SCHEMA_VERSION = 2

_EMPTY_TAGS = {"artist": [], "album": "", "disc": "", "track": "", "title": ""}


@dataclass
class Record:
    hash: str
    tail: str | None
    play_count: int
    first_played_ms: int | None
    last_played_ms: int | None
    added_ms: int | None
    rating_star: int | None


def export(foobar_db: str) -> dict:
    """Read foobar stats + tags and return the JSON-serializable payload.

    One record per foobar entry (no pre-merge): duplicate copies of a recording
    resolve to the same fooyin TrackHash and are aggregated at import time.
    """
    uri = f"file:{foobar_db}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        g = STATS_INDEX_GUID
        rows = conn.execute(
            f"SELECT n.filename, d.value, m.info "
            f"FROM metadb_index_{g} n "
            f"JOIN metadb_index_{g}_data d ON n.key = d.key "
            f"JOIN metadb m ON m.name = n.filename "
            f"WHERE n.filename LIKE '%file://%'"
        )
        records: list[Record] = []
        skipped_bad_blob = 0
        no_tail = 0
        for filename, blob, info in rows:
            stats = parse_stats_blob(blob)
            if stats is None:
                skipped_bad_blob += 1
                continue
            tags = parse_info_tags(info) if info else _EMPTY_TAGS
            h = fooyin_track_hash(
                tags["artist"],
                tags["album"],
                tags["disc"],
                tags["track"],
                tags["title"],
                subsong_from_name(filename),
            )
            tail = path_tail(filename)
            if tail is None:
                no_tail += 1
            records.append(Record(hash=h, tail=tail, **_stats_fields(stats)))
    finally:
        conn.close()

    return {
        "version": SCHEMA_VERSION,
        "source": foobar_db,
        "count": len(records),
        "no_tail": no_tail,
        "skipped_bad_blob": skipped_bad_blob,
        "records": [asdict(r) for r in records],
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
