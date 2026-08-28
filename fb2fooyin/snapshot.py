"""Snapshot stage: fooyin.db -> intermediate JSON.

Read-only against fooyin. Emits one record per track that has playback stats,
carrying both the stored ``TrackHash`` and the case-folded ``hash_norm``, plus
the absolute ``FilePath``. Together those are the keys ``restore`` uses to find
the same recording again after its tags or path have changed.

Timing matters: once a file is edited, fooyin rescans and deletes the old
``Tracks`` row, taking the tags with it. What is left is a ``TrackStats`` row
keyed by a hash nothing can be derived from any more (an orphan). A snapshot is
only useful if it was taken *before* that happens.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime

from .core import (
    SCHEMA_VERSION,
    fooyin_rating_to_star,
    read_contributions,
    read_fooyin_tracks,
    read_trackstats,
)

KIND = "fooyin"


@dataclass
class Record:
    hash: str
    hash_norm: str
    file_path: str
    # Carried so restore's last-resort layer can pair snapshot records against
    # current tracks by (track number, folded title) when both hashes and the
    # path have changed; neither is used for anything else.
    track_number: str
    title: str
    play_count: int
    first_played_ms: int | None
    last_played_ms: int | None
    added_ms: int | None
    rating_star: int | None
    # What a previous ``import`` run contributed to this row, carried so
    # ``restore`` can move the sidecar alongside the stats (otherwise a later
    # foobar import would re-add its own contribution on top).
    contributed: int | None


def _positive(ms: int | None) -> int | None:
    """fooyin writes 0 for "never"; the JSON convention is None."""
    return ms if ms else None


def snapshot(fooyin_db: str, path_prefix: str | None = None) -> dict:
    """Read fooyin's stats + identity and return the JSON-serializable payload.

    One record per (track, stats) pair. Duplicate physical copies of a
    recording share one ``TrackHash`` and therefore one stats row, so they
    produce several records with identical stats but distinct ``file_path`` --
    harmless, because ``restore`` merges with ``max`` rather than summing.

    ``path_prefix`` filters by absolute path. Filtering happens in Python
    rather than via SQL ``LIKE`` so that ``%`` and ``_`` in real directory
    names cannot act as wildcards.
    """
    uri = f"file:{fooyin_db}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        tracks = read_fooyin_tracks(conn)
        stats = read_trackstats(conn)
        contributed = read_contributions(conn)
    finally:
        conn.close()

    records: list[Record] = []
    no_stats = 0
    for t in tracks:
        if path_prefix and not t.file_path.startswith(path_prefix):
            continue
        row = stats.get(t.track_hash)
        if row is None:
            no_stats += 1
            continue
        added, first, last, pc, rating = row
        records.append(
            Record(
                hash=t.track_hash,
                hash_norm=t.norm_hash,
                file_path=t.file_path,
                track_number=t.track,
                title=t.title,
                play_count=pc or 0,
                first_played_ms=_positive(first),
                last_played_ms=_positive(last),
                added_ms=_positive(added),
                rating_star=fooyin_rating_to_star(rating),
                contributed=contributed.get(t.track_hash),
            )
        )

    return {
        "version": SCHEMA_VERSION,
        "kind": KIND,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": fooyin_db,
        "path_prefix": path_prefix,
        "count": len(records),
        "no_stats": no_stats,
        "records": [asdict(r) for r in records],
    }
