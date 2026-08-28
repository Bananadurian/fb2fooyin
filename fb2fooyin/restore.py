"""Restore stage: fooyin snapshot JSON + fooyin.db -> TrackStats.

Repairs the one failure the foobar pipeline cannot: a file whose tags changed,
which makes fooyin mint a new ``TrackHash`` and strand the old stats row as an
orphan. A snapshot taken beforehand still knows that row's identity, so the
stats can be carried across to whatever the recording is called now.

This is deliberately *not* part of ``import``. The foobar import merges
additively (two libraries accumulated plays independently, so they sum);
restoring fooyin from its own snapshot must not add — the same plays would be
counted twice. Keeping the two commands apart keeps the two merge rules apart.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass

from .core import (
    FOOYIN_UNRATED,
    SIDECAR_DDL,
    SIDECAR_TABLE,
    build_norm_index,
    max_pos,
    min_pos,
    norm_tag,
    read_contributions,
    read_fooyin_tracks,
    read_trackstats,
    star_to_fooyin_rating,
)
from .importer import backup_db, begin_write

# Confidence of each resolution layer. Only the deterministic ones may have
# their source row pruned; the heuristic one is reported for human review.
DETERMINISTIC = {"norm", "path"}
_COUNTER = {"norm": "by_norm", "path": "by_path", "fuzzy": "by_fuzzy"}


@dataclass
class Change:
    target_hash: str  # the row that will be written
    source_hash: str  # the (orphaned) row the stats came from
    file_path: str  # snapshot-time path, for the report
    old: tuple  # (added, first, last, play, rating)
    new: tuple
    is_insert: bool
    contributed: int | None  # sidecar value to carry across
    source: str  # "norm" | "path" | "fuzzy"


@dataclass
class RestoreStats:
    intact: int = 0  # identity unchanged — nothing to do
    by_norm: int = 0  # case-folded hash still matches a current track
    by_path: int = 0  # same file path, tags rewritten in place
    by_fuzzy: int = 0  # last resort: track number + folded title within --to
    unresolved: int = 0
    fuzzy_available: bool = True  # False when --to is missing or the set is too big


def merge_restore(old: tuple, incoming: tuple) -> tuple:
    """Merge a snapshot record into a live stats row.

    ``old``/``incoming``/return are ``(added, first, last, play, rating)``.

    Play count takes the **maximum**, not the sum. Restore is a rescue command
    that may well be run twice (a dry run misread, a second album added to the
    same repair); idempotence is worth more than the handful of plays that
    might land on the new row between the file edit and the repair. Every other
    field is a min/max and is idempotent on its own.
    """
    cur_added, cur_first, cur_last, cur_pc, cur_rating = old
    in_added, in_first, in_last, in_pc, in_rating = incoming
    fooyin_rated = cur_rating is not None and cur_rating >= 0
    return (
        min_pos(cur_added, in_added),
        min_pos(cur_first, in_first),
        max_pos(cur_last, in_last),
        max(cur_pc or 0, in_pc or 0),
        cur_rating if fooyin_rated else (in_rating if in_rating is not None else FOOYIN_UNRATED),
    )


def _fuzzy_index(tracks: list, to_prefix: str) -> dict[tuple[str, str], list[str]]:
    """(track number, folded title) -> TrackHash, restricted to one directory.

    The scope is what makes this layer safe: library-wide, "track 1 / intro"
    would collide across hundreds of albums.
    """
    index: dict[tuple[str, str], list[str]] = {}
    for t in tracks:
        if not t.file_path.startswith(to_prefix):
            continue
        bucket = index.setdefault((t.track.strip(), norm_tag(t.title)), [])
        if t.track_hash not in bucket:
            bucket.append(t.track_hash)
    return index


def plan_restore(
    conn: sqlite3.Connection,
    records: list[dict],
    to_prefix: str | None = None,
    fuzzy_limit: int = 100,
) -> tuple[list[Change], list[str], RestoreStats]:
    """Compute the restore writes without touching the database.

    Resolution order, most exact first::

        hash still present in Tracks?      -> skip, the identity never broke
          -> case-folded hash matches?     -> move  (tags drifted in case)
            -> same FilePath?              -> move  (re-tagged in place)
              -> track number + title?     -> move  (needs --to, small sets only)
                -> unresolved

    Returns ``(changes, unresolved_paths, stats)``.
    """
    tracks = read_fooyin_tracks(conn)
    live_hashes = {t.track_hash for t in tracks}
    norm_index = build_norm_index(tracks)
    path_index: dict[str, list[str]] = {}
    for t in tracks:
        bucket = path_index.setdefault(t.file_path, [])
        if t.track_hash not in bucket:
            bucket.append(t.track_hash)

    fuzzy_ok = bool(to_prefix) and len(records) < fuzzy_limit
    fuzzy_index = _fuzzy_index(tracks, to_prefix) if fuzzy_ok else {}

    stats = read_trackstats(conn)
    contributed = read_contributions(conn)

    changes: list[Change] = []
    unresolved: list[str] = []
    report = RestoreStats(fuzzy_available=fuzzy_ok)
    # Several snapshot records can name one live row (duplicate copies share a
    # TrackHash); merge them into one write so the result does not depend on
    # record order.
    planned: dict[str, Change] = {}

    for rec in records:
        if rec.get("hash") in live_hashes:
            report.intact += 1
            continue
        targets, source = [], None
        norm = norm_index.get(rec.get("hash_norm"))
        if norm:
            targets, source = norm, "norm"
        elif rec.get("file_path") in path_index:
            targets, source = path_index[rec["file_path"]], "path"
        elif fuzzy_ok:
            key = ((rec.get("track_number") or "").strip(), norm_tag(rec.get("title") or ""))
            hit = fuzzy_index.get(key)
            if hit:
                targets, source = hit, "fuzzy"
        if not targets:
            report.unresolved += 1
            unresolved.append(rec.get("file_path"))
            continue
        counter = _COUNTER[source]
        setattr(report, counter, getattr(report, counter) + 1)

        incoming = (
            rec["added_ms"],
            rec["first_played_ms"],
            rec["last_played_ms"],
            rec["play_count"],
            star_to_fooyin_rating(rec["rating_star"]) if rec["rating_star"] is not None else None,
        )
        for h in targets:
            existing = planned.get(h)
            base = existing.new if existing else stats.get(h)
            is_insert = existing.is_insert if existing else base is None
            old = base if base is not None else (None, None, None, 0, FOOYIN_UNRATED)
            new = merge_restore(old, incoming)
            planned[h] = Change(
                target_hash=h,
                source_hash=rec["hash"],
                file_path=rec.get("file_path") or "",
                old=existing.old if existing else old,
                new=new,
                is_insert=is_insert,
                contributed=rec.get("contributed"),
                source=source,
            )

    for h, ch in planned.items():
        if ch.is_insert or ch.new != ch.old:
            changes.append(ch)
    return changes, unresolved, report


def apply_restore(
    fooyin_db: str, changes: list[Change], prune_moved: bool = False
) -> str:
    """Write the restore inside one transaction. Returns the backup path.

    With ``prune_moved`` the source orphan row (and its sidecar row) is deleted
    once its stats have been written elsewhere — completing a move rather than
    leaving a copy. Only rows resolved deterministically are pruned; a fuzzy
    match is reviewed by eye, and "I looked at it" is not a licence to destroy
    the only remaining copy.
    """
    backup = backup_db(fooyin_db)
    conn = begin_write(fooyin_db)
    try:
        conn.execute(SIDECAR_DDL)
        now_ms = int(time.time() * 1000)
        live = {h for (h,) in conn.execute(
            "SELECT TrackHash FROM Tracks WHERE TrackHash IS NOT NULL"
        )}
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
                (ch.target_hash, added, first, last, pc, rating),
            )
            # Carry the sidecar across, but never over an existing one: if the
            # target already has a contribution recorded, that import knew what
            # it wrote and the snapshot's number is the stale one.
            if ch.contributed is not None:
                conn.execute(
                    f"INSERT OR IGNORE INTO {SIDECAR_TABLE} "
                    f"(TrackHash, ContributedPlayCount, ImportedAt) VALUES (?, ?, ?)",
                    (ch.target_hash, ch.contributed, now_ms),
                )
            if (
                prune_moved
                and ch.source in DETERMINISTIC
                and ch.source_hash != ch.target_hash
                and ch.source_hash not in live
            ):
                conn.execute(
                    "DELETE FROM TrackStats WHERE TrackHash = ?", (ch.source_hash,)
                )
                conn.execute(
                    f"DELETE FROM {SIDECAR_TABLE} WHERE TrackHash = ?", (ch.source_hash,)
                )
        conn.commit()
    finally:
        conn.close()
    return backup


# --- orphans ------------------------------------------------------------

# A stats row whose hash is no longer in Tracks. Every broken identity ends up
# here, so the list doubles as the only alarm this tool has: a non-empty list
# means something in the library changed identity without being repaired.


@dataclass
class Orphan:
    track_hash: str
    play_count: int
    contributed: int | None
    last_played_ms: int | None
    known_path: str | None = None  # recovered from a payload, when one is given


def list_orphans(
    conn: sqlite3.Connection,
    known: dict[str, str] | None = None,
    include_empty: bool = False,
) -> list[Orphan]:
    """Stats rows with no live track, newest activity first.

    ``known`` maps hash -> path (from a snapshot or a foobar export) to give
    each orphan a human-readable identity again. Rows with no plays carry no
    data and no information, so they are hidden unless ``include_empty``.
    """
    contributed = read_contributions(conn)
    rows = conn.execute(
        "SELECT TrackHash, PlayCount, LastPlayed FROM TrackStats "
        "WHERE TrackHash NOT IN (SELECT TrackHash FROM Tracks WHERE TrackHash IS NOT NULL)"
    )
    out = [
        Orphan(
            track_hash=h,
            play_count=pc or 0,
            contributed=contributed.get(h),
            last_played_ms=last or None,
            known_path=(known or {}).get(h),
        )
        for h, pc, last in rows
        if include_empty or pc
    ]
    out.sort(key=lambda o: (o.last_played_ms or 0), reverse=True)
    return out


def missing_library_roots(conn: sqlite3.Connection) -> list[str]:
    """Configured library paths that are not present on disk right now.

    An unmounted drive or a library folder that moved makes fooyin drop every
    track from ``Tracks``, which turns the whole library into orphans. Pruning
    then would delete all playback history in one command, so a caller that
    deletes must refuse while this is non-empty.
    """
    try:
        paths = [p for (p,) in conn.execute("SELECT Path FROM Libraries")]
    except sqlite3.OperationalError:
        return []
    return [p for p in paths if not os.path.isdir(p)]


def prune_orphans(fooyin_db: str, hashes: list[str]) -> str:
    """Delete the named orphan rows (and their sidecar rows). Returns the backup path."""
    backup = backup_db(fooyin_db)
    conn = begin_write(fooyin_db)
    try:
        missing = missing_library_roots(conn)
        if missing:
            raise RuntimeError(
                "refusing to prune: these library roots are not on disk right now, "
                "so their tracks look like orphans — "
                f"{', '.join(missing)}"
            )
        conn.execute(SIDECAR_DDL)  # so the sidecar delete works on a never-imported db
        for h in hashes:
            conn.execute("DELETE FROM TrackStats WHERE TrackHash = ?", (h,))
            conn.execute(f"DELETE FROM {SIDECAR_TABLE} WHERE TrackHash = ?", (h,))
        conn.commit()
    finally:
        conn.close()
    return backup
