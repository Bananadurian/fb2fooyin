"""Inspect stage: show foobar vs fooyin stats side by side for a track.

Read-only on both databases. For quick manual cross-checking of a specific
song before/after an import — no writes, no JSON.
"""

from __future__ import annotations

import sqlite3
import time

from .core import (
    FOOYIN_UNRATED,
    STATS_INDEX_GUID,
    fooyin_track_hash,
    parse_info_tags,
    parse_stats_blob,
    path_tail,
    star_to_fooyin_rating,
    subsong_from_name,
)
from .importer import merge_one

_FIELD_W = 13
_FB_W = 24

_EMPTY_TAGS = {"artist": [], "album": "", "disc": "", "track": "", "title": ""}


def _fmt_ms(ms: int | None) -> str:
    if not ms:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000))


def _short(h: str | None) -> str:
    """Shorten a 32-hex hash so the two-column layout stays aligned."""
    return f"{h[:12]}…" if h else "—"


def _foobar_rating(blob: bytes) -> str:
    byte = blob[32]
    star = parse_stats_blob(blob).rating_star
    return f"{star}★ (0x{byte:02X})" if star else f"unrated (0x{byte:02X})"


def _fooyin_rating(rating: float | None) -> str:
    if rating is None:
        return "— (no stats row)"
    if rating < 0:
        return "unrated (-1.0)"
    return f"{rating * 5:.1f}★ ({rating:g})"


def _gather_foobar(foobar_db: str, query: str) -> dict[str, tuple]:
    conn = sqlite3.connect(f"file:{foobar_db}?mode=ro", uri=True)
    g = STATS_INDEX_GUID
    out: dict[str, tuple] = {}
    try:
        for filename, blob, info in conn.execute(
            f"SELECT n.filename, d.value, m.info FROM metadb_index_{g} n "
            f"JOIN metadb_index_{g}_data d ON n.key = d.key "
            f"JOIN metadb m ON m.name = n.filename "
            f"WHERE n.filename LIKE '%file://%'"
        ):
            tail = path_tail(filename)
            if tail and query in tail:
                tags = parse_info_tags(info) if info else _EMPTY_TAGS
                h = fooyin_track_hash(
                    tags["artist"],
                    tags["album"],
                    tags["disc"],
                    tags["track"],
                    tags["title"],
                    subsong_from_name(filename),
                )
                out[tail] = (parse_stats_blob(blob), blob, filename, h)
    finally:
        conn.close()
    return out


def _gather_fooyin(fooyin_db: str, query: str) -> dict[str, dict]:
    conn = sqlite3.connect(f"file:{fooyin_db}?mode=ro", uri=True)
    out: dict[str, dict] = {}
    try:
        # Previous per-hash contributions (sidecar may not exist yet), so the
        # merged preview matches what an import would actually write.
        prev: dict[str, int] = {}
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='_fb2fooyin_import'"
        ).fetchone():
            prev = {
                h: c
                for h, c in conn.execute(
                    "SELECT TrackHash, ContributedPlayCount FROM _fb2fooyin_import"
                )
            }
        for path, h, added, first, last, pc, rating in conn.execute(
            "SELECT t.FilePath, t.TrackHash, s.AddedDate, s.FirstPlayed, "
            "s.LastPlayed, s.PlayCount, s.Rating "
            "FROM Tracks t LEFT JOIN TrackStats s ON t.TrackHash = s.TrackHash"
        ):
            tail = path_tail(path)
            if tail and query in tail:
                out[tail] = {
                    "hash": h,
                    "added": added,
                    "first": first,
                    "last": last,
                    "pc": pc,
                    "rating": rating,
                    "prev": prev.get(h, 0),
                    "path": path,
                }
    finally:
        conn.close()
    return out


def _merged(stats, y: dict | None) -> tuple | None:
    """The (added, first, last, pc, rating) an import would write for this
    track, or None when there's no foobar entry to merge in."""
    if stats is None:
        return None
    fb_rating = (
        star_to_fooyin_rating(stats.rating_star) if stats.rating_star is not None else None
    )
    incoming = (
        stats.added_ms,
        stats.first_played_ms,
        stats.last_played_ms,
        stats.play_count,
        fb_rating,
    )
    if y is None:
        old = (None, None, None, 0, FOOYIN_UNRATED)
        prev = 0
    else:
        old = (
            y["added"],
            y["first"],
            y["last"],
            y["pc"] or 0,
            y["rating"] if y["rating"] is not None else FOOYIN_UNRATED,
        )
        prev = y["prev"]
    return merge_one(old, incoming, prev)


def _row(field: str, fb: object, fy: object, merged: object = "") -> str:
    return f"  {field:<{_FIELD_W}}{str(fb):<{_FB_W}}{str(fy):<{_FB_W}}{merged}"


def render(foobar_db: str, fooyin_db: str, query: str, limit: int) -> str:
    q = query.lower()
    fb = _gather_foobar(foobar_db, q)
    fy = _gather_fooyin(fooyin_db, q)
    tails = sorted(set(fb) | set(fy))
    if not tails:
        return f'no track matches "{query}" in either database.'

    blocks = [f'{len(tails)} match(es) for "{query}":\n']
    for tail in tails[:limit]:
        f = fb.get(tail)
        y = fy.get(tail)
        stats = f[0] if f else None
        m = _merged(stats, y)  # (added, first, last, pc, rating) an import would write
        blocks.append(tail)
        blocks.append(_row("field", "foobar", "fooyin", "merged"))
        blocks.append(
            _row(
                "play_count",
                stats.play_count if stats else "—",
                y["pc"] if y else "—",
                m[3] if m else "—",
            )
        )
        blocks.append(
            _row(
                "rating",
                _foobar_rating(f[1]) if f else "—",
                _fooyin_rating(y["rating"]) if y else "— (not in fooyin)",
                _fooyin_rating(m[4]) if m else "—",
            )
        )
        blocks.append(
            _row(
                "first_played",
                _fmt_ms(stats.first_played_ms) if stats else "—",
                _fmt_ms(y["first"]) if y else "—",
                _fmt_ms(m[1]) if m else "—",
            )
        )
        blocks.append(
            _row(
                "last_played",
                _fmt_ms(stats.last_played_ms) if stats else "—",
                _fmt_ms(y["last"]) if y else "—",
                _fmt_ms(m[2]) if m else "—",
            )
        )
        blocks.append(
            _row(
                "added",
                _fmt_ms(stats.added_ms) if stats else "—",
                _fmt_ms(y["added"]) if y else "—",
                _fmt_ms(m[0]) if m else "—",
            )
        )
        fb_hash = f[3] if f else None
        fy_hash = y["hash"] if y else None
        blocks.append(_row("hash", _short(fb_hash), _short(fy_hash)))
        if fb_hash and fy_hash and fb_hash == fy_hash:
            how = "matched BY HASH ✓ (path-independent)"
        elif f and y:
            how = "matched BY TAIL (hashes differ)"
        else:
            how = "—"
        blocks.append(f"  {'match':<{_FIELD_W}}{how}")
        blocks.append("")
    if len(tails) > limit:
        blocks.append(f"… {len(tails) - limit} more (raise --limit to see them)")
    return "\n".join(blocks)
