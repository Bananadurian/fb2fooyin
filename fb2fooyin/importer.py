"""Import stage: intermediate JSON + fooyin.db -> TrackStats.

Matches records to fooyin tracks by content hash (exact, then primary-artist,
then case-folded), falling back to the album-relative path tail, and merges the
foobar stats into TrackStats. Play counts are merged idempotently via a sidecar
table that records each run's contribution.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from dataclasses import dataclass, field

from .core import (
    FOOYIN_UNRATED,
    SIDECAR_DDL,
    SIDECAR_TABLE,
    build_norm_index,
    max_pos as _max_pos,
    min_pos as _min_pos,
    path_tail,
    read_contributions,
    read_fooyin_tracks,
    read_trackstats,
    star_to_fooyin_rating,
)

_SIDECAR = SIDECAR_TABLE
_SIDECAR_DDL = SIDECAR_DDL


@dataclass
class Change:
    track_hash: str
    tail: str
    old: tuple  # (added, first, last, play, rating)
    new: tuple
    is_insert: bool
    contributed: int  # aggregated foobar play count this run wrote (for the sidecar)
    source: str  # how the row was resolved: "hash" | "hash_primary" | "norm" | "tail"


@dataclass
class MatchStats:
    """How the run's records resolved to fooyin tracks (for a transparent report)."""

    by_hash: int = 0  # matched by recomputed full-artist TrackHash (path-independent)
    by_primary: int = 0  # matched by the primary-artist-only hash (multi-artist fallback)
    by_norm: int = 0  # matched by the case-folded hash (tags drifted in case only)
    by_tail: int = 0  # matched only by album-relative path tail (last-resort fallback)
    unmatched: int = 0  # no hash and no tail hit (track absent from fooyin)
    # Tails whose duplicate foobar entries disagreed on play count (see
    # dedupe_records); surfaced so the max-not-sum choice stays reviewable.
    dedup_conflicts: list[str] = field(default_factory=list)


# Confidence ranking of a resolution source; when several records touch one
# fooyin row it keeps the highest-confidence one (content hash beats path tail).
_SOURCE_RANK = {"hash": 4, "hash_primary": 3, "norm": 2, "tail": 1}


# --- small merge helpers (0/None == "unknown") --------------------------
# _min_pos / _max_pos are core.min_pos / core.max_pos, aliased above so both
# the import and restore merges share one definition.


def merge_one(
    old: tuple,
    incoming: tuple,
    prev_contributed: int = 0,
    keep_fooyin_rating: bool = False,
) -> tuple:
    """Merge one foobar contribution into a fooyin stats row.

    ``old``/``incoming``/return are ``(added, first, last, play, rating)``. This
    is the single source of truth for the field merge rules — used by
    ``plan_changes`` for the real write and by ``inspect`` for its preview.
    """
    cur_added, cur_first, cur_last, cur_pc, cur_rating = old
    in_added, in_first, in_last, in_pc, in_rating = incoming

    # Idempotent additive play count: back out our previous contribution so
    # re-runs and growing foobar counts both land correctly, while plays fooyin
    # itself recorded between runs survive.
    new_pc = max(0, (cur_pc or 0) - prev_contributed + in_pc)
    new_first = _min_pos(cur_first, in_first)
    new_last = _max_pos(cur_last, in_last)
    new_added = _min_pos(cur_added, in_added)
    fooyin_rated = cur_rating is not None and cur_rating >= 0
    if keep_fooyin_rating and fooyin_rated:
        new_rating = cur_rating
    elif in_rating is not None:
        new_rating = in_rating
    elif fooyin_rated:
        new_rating = cur_rating
    else:
        new_rating = FOOYIN_UNRATED
    return (new_added, new_first, new_last, new_pc, new_rating)


# --- record de-duplication ----------------------------------------------

# foobar's metadb accumulates an entry per *path spelling*, so a library whose
# root was renamed holds the same physical file several times over (this one
# has D:\11_music, D:\11_MusicLib and an exttag_off:// variant). Those ghosts
# are not the "duplicate copies" plan_changes is meant to sum -- summing them
# multiplies every play count by the number of stale roots.
#
# The discriminator is the album-relative tail: same identity AND same tail is
# one file seen twice; same identity but a different tail is a genuine second
# copy in another album folder, which still sums (see DESIGN §5).


def dedupe_records(records: list[dict]) -> tuple[list[dict], list[str]]:
    """Collapse records that describe one physical file into one.

    Returns ``(records, conflicts)`` where ``conflicts`` lists the tails whose
    duplicates disagreed on play count -- those take the maximum rather than
    the sum (the stale roots are mirrors of one history, not two histories),
    and are reported so the choice stays visible.
    """
    groups: dict[tuple, dict] = {}
    order: list[tuple] = []
    conflicting: set[tuple] = set()
    for rec in records:
        key = (rec.get("hash"), rec.get("tail"))
        prev = groups.get(key)
        if prev is None:
            groups[key] = dict(rec)
            order.append(key)
            continue
        if prev["play_count"] != rec["play_count"]:
            conflicting.add(key)
        prev["play_count"] = max(prev["play_count"], rec["play_count"])
        prev["first_played_ms"] = _min_pos(prev["first_played_ms"], rec["first_played_ms"])
        prev["last_played_ms"] = _max_pos(prev["last_played_ms"], rec["last_played_ms"])
        prev["added_ms"] = _min_pos(prev["added_ms"], rec["added_ms"])
        prev["rating_star"] = _max_pos(prev["rating_star"], rec["rating_star"])
    conflicts = [groups[k].get("tail") or (k[0] or "") for k in order if k in conflicting]
    return [groups[k] for k in order], conflicts


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


def _resolve(
    rec: dict,
    fooyin_hashes: set[str],
    norm_index: dict[str, list[str]],
    tail_index: dict[str, list[str]],
) -> tuple[list[str], str | None]:
    """Resolve a record to target fooyin TrackHash(es), most exact key first.

    Returns ``(hashes, source)`` with ``source`` in
    ``{"hash", "hash_primary", "norm", "tail", None}``. The full-artist content
    hash wins; a primary-artist-only hash (for multi-artist tracks fooyin filed
    under the lead artist) is tried next; then the case-folded hash (for tags
    that drifted in letter case); otherwise fall back to the album-relative
    path tail (which may resolve to several duplicate copies).
    """
    h = rec.get("hash")
    if h and h in fooyin_hashes:
        return [h], "hash"
    hp = rec.get("hash_primary")
    if hp and hp in fooyin_hashes:
        return [hp], "hash_primary"
    hn = rec.get("hash_norm")
    norm = norm_index.get(hn) if hn else None
    if norm:
        return norm, "norm"
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

    Records that describe the same physical file under several foobar path
    spellings are collapsed first (``dedupe_records``), then each is resolved
    to its fooyin TrackHash by content hash, case-folded hash, and finally
    album-relative path tail (see ``_resolve``). With ``keep_fooyin_rating`` a
    rating already set in fooyin is never overwritten by foobar (foobar still
    fills in ratings fooyin lacks).

    Returns ``(changes, unmatched_tails, match_stats)``.
    """
    tracks = read_fooyin_tracks(conn)
    fooyin_hashes = {t.track_hash for t in tracks}
    norm_index = build_norm_index(tracks)
    tail_index = _build_tail_index(conn)
    stats = read_trackstats(conn)
    prev = read_contributions(conn)

    records, dedup_conflicts = dedupe_records(records)

    # 1. Aggregate incoming records by the fooyin TrackHash they resolve to.
    #    Duplicate physical copies of one recording share a single TrackHash
    #    (fooyin dedups by content), so several records can target one row.
    #    Combine them once here — otherwise they race to write the same row and
    #    the result is non-deterministic and non-idempotent.
    agg: dict[str, dict] = {}
    unmatched: list[str] = []
    match = MatchStats(dedup_conflicts=dedup_conflicts)
    for rec in records:
        hashes, source = _resolve(rec, fooyin_hashes, norm_index, tail_index)
        if not hashes:
            unmatched.append(rec.get("tail"))
            match.unmatched += 1
            continue
        if source == "hash":
            match.by_hash += 1
        elif source == "hash_primary":
            match.by_primary += 1
        elif source == "norm":
            match.by_norm += 1
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
                    "source": source,
                }
            else:
                a["pc"] += rec["play_count"]  # summed copies (unplayed copies add 0)
                a["first"] = _min_pos(a["first"], rec["first_played_ms"])
                a["last"] = _max_pos(a["last"], rec["last_played_ms"])
                a["added"] = _min_pos(a["added"], rec["added_ms"])
                a["rating"] = _max_pos(a["rating"], rec_rating)
                if _SOURCE_RANK[source] > _SOURCE_RANK[a["source"]]:
                    a["source"] = source

    # 2. Merge each hash against the current row + sidecar exactly once.
    changes: list[Change] = []
    for h, a in agg.items():
        cur = stats.get(h)
        is_insert = cur is None
        old = cur if cur is not None else (None, None, None, 0, FOOYIN_UNRATED)
        incoming = (a["added"], a["first"], a["last"], a["pc"], a["rating"])
        new = merge_one(old, incoming, prev.get(h, 0), keep_fooyin_rating)
        if is_insert or new != old:
            changes.append(Change(h, a["tail"], old, new, is_insert, a["pc"], a["source"]))
    return changes, unmatched, match


# --- apply --------------------------------------------------------------


def backup_db(fooyin_db: str) -> str:
    """Copy fooyin.db next to itself with a timestamp. Returns the backup path."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = f"{fooyin_db}.bak-{stamp}"
    shutil.copy2(fooyin_db, dest)
    return dest


def begin_write(fooyin_db: str) -> sqlite3.Connection:
    """Open fooyin.db and claim the write lock, refusing if fooyin holds it.

    Shared by every writing command so "close fooyin first" is enforced in one
    place rather than re-implemented per command.
    """
    conn = sqlite3.connect(fooyin_db, timeout=1.0)
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        conn.close()
        raise RuntimeError(
            f"fooyin.db is locked — close fooyin before writing (sqlite: {exc})"
        ) from exc
    return conn


def apply_changes(fooyin_db: str, changes: list[Change]) -> str:
    """Write changes inside one transaction. Returns the backup path.

    Refuses to run if fooyin holds the database lock.
    """
    backup = backup_db(fooyin_db)
    conn = begin_write(fooyin_db)
    try:
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


def load_payload(json_path: str, expect_kind: str | None = None) -> dict:
    """Read an intermediate JSON payload, optionally asserting its producer.

    The two producers (foobar ``export``, fooyin ``snapshot``) share the record
    schema but not the merge semantics, so feeding one to the other's consumer
    would silently write wrong numbers. The ``kind`` tag makes that a loud
    failure instead. Payloads older than schema v5 carry no tag and are only
    accepted where no kind is required.
    """
    with open(json_path, encoding="utf-8") as f:
        payload = json.load(f)
    if expect_kind is not None:
        kind = payload.get("kind")
        if kind != expect_kind:
            raise RuntimeError(
                f"{json_path} was produced by {kind or 'an older version (no kind tag)'}, "
                f"but this command needs a '{expect_kind}' payload"
            )
    return payload
