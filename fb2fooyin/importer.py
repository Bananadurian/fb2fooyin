"""Import stage: intermediate JSON + fooyin.db -> TrackStats.

Matches records to fooyin tracks by album-relative path tail, resolves the
tail to fooyin's content-based TrackHash, and merges the foobar stats into
TrackStats. Play counts are merged idempotently via a sidecar table that
records each run's contribution.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from dataclasses import dataclass

from .core import FOOYIN_UNRATED, path_tail, star_to_fooyin_rating

_SIDECAR = "_fb2fooyin_import"
_SIDECAR_DDL = f"""
CREATE TABLE IF NOT EXISTS {_SIDECAR} (
    TrackHash TEXT PRIMARY KEY,
    ContributedPlayCount INTEGER NOT NULL,
    ImportedAt INTEGER NOT NULL
)
"""


@dataclass
class Change:
    track_hash: str
    tail: str
    old: tuple  # (play, first, last, added, rating)
    new: tuple
    is_insert: bool


# --- small merge helpers (0/None == "unknown") --------------------------


def _min_pos(*vals: int | None) -> int | None:
    present = [v for v in vals if v]  # drop None and 0
    return min(present) if present else None


def _max_pos(*vals: int | None) -> int | None:
    present = [v for v in vals if v]
    return max(present) if present else None


# --- fooyin reads -------------------------------------------------------


def _build_tail_index(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Map album-relative tail -> list of distinct TrackHash."""
    index: dict[str, list[str]] = {}
    for path, h in conn.execute("SELECT FilePath, TrackHash FROM Tracks"):
        if h is None:
            continue
        tail = path_tail(path)
        if tail is None:
            continue
        bucket = index.setdefault(tail, [])
        if h not in bucket:
            bucket.append(h)
    return index


def _load_trackstats(conn: sqlite3.Connection) -> dict[str, tuple]:
    """hash -> (AddedDate, FirstPlayed, LastPlayed, PlayCount, Rating)."""
    out: dict[str, tuple] = {}
    for h, added, first, last, pc, rating in conn.execute(
        "SELECT TrackHash, AddedDate, FirstPlayed, LastPlayed, PlayCount, Rating "
        "FROM TrackStats"
    ):
        out[h] = (added, first, last, pc, rating)
    return out


def _load_prev_contrib(conn: sqlite3.Connection) -> dict[str, int]:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_SIDECAR,)
    ).fetchone()
    if not exists:
        return {}
    return {
        h: c
        for h, c in conn.execute(
            f"SELECT TrackHash, ContributedPlayCount FROM {_SIDECAR}"
        )
    }


# --- planning -----------------------------------------------------------


def plan_changes(conn: sqlite3.Connection, records: list[dict]) -> tuple[list[Change], list[str]]:
    """Compute the merged TrackStats writes without touching the database.

    Returns ``(changes, unmatched_tails)``.
    """
    tail_index = _build_tail_index(conn)
    stats = _load_trackstats(conn)
    prev = _load_prev_contrib(conn)

    changes: list[Change] = []
    unmatched: list[str] = []
    for rec in records:
        tail = rec["tail"]
        hashes = tail_index.get(tail)
        if not hashes:
            unmatched.append(tail)
            continue
        rec_pc = rec["play_count"]
        rec_first = rec["first_played_ms"]
        rec_last = rec["last_played_ms"]
        rec_added = rec["added_ms"]
        rec_star = rec["rating_star"]
        rec_rating = star_to_fooyin_rating(rec_star) if rec_star is not None else None

        for h in hashes:
            cur = stats.get(h)
            is_insert = cur is None
            cur_added, cur_first, cur_last, cur_pc, cur_rating = (
                cur if cur is not None else (None, None, None, 0, FOOYIN_UNRATED)
            )
            contributed = prev.get(h, 0)

            # Idempotent additive play count: back out our previous contribution
            # so re-runs and growing foobar counts both land correctly, while
            # plays fooyin itself recorded between runs survive.
            new_pc = max(0, (cur_pc or 0) - contributed + rec_pc)
            new_first = _min_pos(cur_first, rec_first)
            new_last = _max_pos(cur_last, rec_last)
            new_added = _min_pos(cur_added, rec_added)
            if rec_rating is not None:
                new_rating = rec_rating
            elif cur_rating is not None and cur_rating >= 0:
                new_rating = cur_rating
            else:
                new_rating = FOOYIN_UNRATED

            new = (new_added, new_first, new_last, new_pc, new_rating)
            old = (cur_added, cur_first, cur_last, cur_pc, cur_rating)
            if is_insert or new != old:
                changes.append(Change(h, tail, old, new, is_insert))
    return changes, unmatched


# --- apply --------------------------------------------------------------


def _backup(fooyin_db: str) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = f"{fooyin_db}.bak-{stamp}"
    shutil.copy2(fooyin_db, dest)
    return dest


def apply_changes(fooyin_db: str, records: list[dict], changes: list[Change]) -> str:
    """Write changes inside one transaction. Returns the backup path.

    Refuses to run if fooyin holds the database lock.
    """
    backup = _backup(fooyin_db)
    conn = sqlite3.connect(fooyin_db, timeout=1.0)
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise RuntimeError(
                "fooyin.db is locked — close fooyin before importing "
                f"(sqlite: {exc})"
            ) from exc

        conn.execute(_SIDECAR_DDL)
        now_ms = int(time.time() * 1000)
        rec_pc_by_tail = {r["tail"]: r["play_count"] for r in records}

        for ch in changes:
            added, first, last, pc, rating = ch.new
            conn.execute(
                """
                INSERT INTO TrackStats
                    (TrackHash, AddedDate, FirstPlayed, LastPlayed, PlayCount, Rating)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(TrackHash) DO UPDATE SET
                    AddedDate   = excluded.AddedDate,
                    FirstPlayed = excluded.FirstPlayed,
                    LastPlayed  = excluded.LastPlayed,
                    PlayCount   = excluded.PlayCount,
                    Rating      = excluded.Rating
                """,
                (ch.track_hash, added, first, last, pc, rating),
            )
            conn.execute(
                f"""
                INSERT INTO {_SIDECAR} (TrackHash, ContributedPlayCount, ImportedAt)
                VALUES (?, ?, ?)
                ON CONFLICT(TrackHash) DO UPDATE SET
                    ContributedPlayCount = excluded.ContributedPlayCount,
                    ImportedAt           = excluded.ImportedAt
                """,
                (ch.track_hash, rec_pc_by_tail.get(ch.tail, 0), now_ms),
            )
        conn.commit()
    finally:
        conn.close()
    return backup


def load_payload(json_path: str) -> dict:
    with open(json_path, encoding="utf-8") as f:
        return json.load(f)
