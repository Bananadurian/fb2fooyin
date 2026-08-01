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
    old: tuple  # (added, first, last, play, rating)
    new: tuple
    is_insert: bool
    contributed: int  # aggregated foobar play count this run wrote (for the sidecar)


@dataclass
class MatchStats:
    """How the run's records resolved to fooyin tracks (for a transparent report)."""

    by_hash: int = 0  # matched by recomputed fooyin TrackHash (path-independent)
    by_tail: int = 0  # matched only by album-relative path tail (hash fallback)
    unmatched: int = 0  # no hash and no tail hit (track absent from fooyin)


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


def _load_fooyin_hashes(conn: sqlite3.Connection) -> set[str]:
    """The set of fooyin content hashes, for primary (path-independent) matching."""
    return {
        h for (h,) in conn.execute(
            "SELECT TrackHash FROM Tracks WHERE TrackHash IS NOT NULL"
        )
    }


def _resolve(
    rec: dict, fooyin_hashes: set[str], tail_index: dict[str, list[str]]
) -> tuple[list[str], str | None]:
    """Resolve a record to target fooyin TrackHash(es): hash first, then tail.

    Returns ``(hashes, source)`` with ``source`` in ``{"hash", "tail", None}``.
    A recomputed hash present in fooyin wins; otherwise fall back to the
    album-relative path tail (which may resolve to several duplicate copies).
    """
    h = rec.get("hash")
    if h and h in fooyin_hashes:
        return [h], "hash"
    tail = rec.get("tail")
    tails = tail_index.get(tail) if tail else None
    if tails:
        return tails, "tail"
    return [], None


# --- planning -----------------------------------------------------------


def plan_changes(
    conn: sqlite3.Connection,
    records: list[dict],
    keep_fooyin_rating: bool = False,
) -> tuple[list[Change], list[str], MatchStats]:
    """Compute the merged TrackStats writes without touching the database.

    Each record is resolved to its fooyin TrackHash by content hash first, then
    by album-relative path tail (see ``_resolve``). With ``keep_fooyin_rating`` a
    rating already set in fooyin is never overwritten by foobar (foobar still
    fills in ratings fooyin lacks).

    Returns ``(changes, unmatched_tails, match_stats)``.
    """
    fooyin_hashes = _load_fooyin_hashes(conn)
    tail_index = _build_tail_index(conn)
    stats = _load_trackstats(conn)
    prev = _load_prev_contrib(conn)

    # 1. Aggregate incoming records by the fooyin TrackHash they resolve to.
    #    Duplicate physical copies of one recording share a single TrackHash
    #    (fooyin dedups by content), so several records can target one row.
    #    Combine them once here — otherwise they race to write the same row and
    #    the result is non-deterministic and non-idempotent.
    agg: dict[str, dict] = {}
    unmatched: list[str] = []
    match = MatchStats()
    for rec in records:
        hashes, source = _resolve(rec, fooyin_hashes, tail_index)
        if not hashes:
            unmatched.append(rec.get("tail"))
            match.unmatched += 1
            continue
        if source == "hash":
            match.by_hash += 1
        else:
            match.by_tail += 1
        rec_star = rec["rating_star"]
        rec_rating = star_to_fooyin_rating(rec_star) if rec_star is not None else None
        for h in hashes:
            a = agg.get(h)
            if a is None:
                agg[h] = {
                    "pc": rec["play_count"],
                    "first": rec["first_played_ms"],
                    "last": rec["last_played_ms"],
                    "added": rec["added_ms"],
                    "rating": rec_rating,
                    "tail": rec.get("tail") or h,
                }
            else:
                a["pc"] += rec["play_count"]  # summed copies (unplayed copies add 0)
                a["first"] = _min_pos(a["first"], rec["first_played_ms"])
                a["last"] = _max_pos(a["last"], rec["last_played_ms"])
                a["added"] = _min_pos(a["added"], rec["added_ms"])
                a["rating"] = _max_pos(a["rating"], rec_rating)

    # 2. Merge each hash against the current row + sidecar exactly once.
    changes: list[Change] = []
    for h, a in agg.items():
        cur = stats.get(h)
        is_insert = cur is None
        cur_added, cur_first, cur_last, cur_pc, cur_rating = (
            cur if cur is not None else (None, None, None, 0, FOOYIN_UNRATED)
        )
        contributed = prev.get(h, 0)

        # Idempotent additive play count: back out our previous contribution so
        # re-runs and growing foobar counts both land correctly, while plays
        # fooyin itself recorded between runs survive.
        new_pc = max(0, (cur_pc or 0) - contributed + a["pc"])
        new_first = _min_pos(cur_first, a["first"])
        new_last = _max_pos(cur_last, a["last"])
        new_added = _min_pos(cur_added, a["added"])
        fooyin_rated = cur_rating is not None and cur_rating >= 0
        if keep_fooyin_rating and fooyin_rated:
            new_rating = cur_rating
        elif a["rating"] is not None:
            new_rating = a["rating"]
        elif fooyin_rated:
            new_rating = cur_rating
        else:
            new_rating = FOOYIN_UNRATED

        new = (new_added, new_first, new_last, new_pc, new_rating)
        old = (cur_added, cur_first, cur_last, cur_pc, cur_rating)
        if is_insert or new != old:
            changes.append(Change(h, a["tail"], old, new, is_insert, a["pc"]))
    return changes, unmatched, match


# --- apply --------------------------------------------------------------


def _backup(fooyin_db: str) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = f"{fooyin_db}.bak-{stamp}"
    shutil.copy2(fooyin_db, dest)
    return dest


def apply_changes(fooyin_db: str, changes: list[Change]) -> str:
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
                (ch.track_hash, ch.contributed, now_ms),
            )
        conn.commit()
    finally:
        conn.close()
    return backup


def load_payload(json_path: str) -> dict:
    with open(json_path, encoding="utf-8") as f:
        return json.load(f)
