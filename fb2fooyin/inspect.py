"""Inspect stage: show foobar vs fooyin stats side by side for a track.

Read-only on both databases. For quick manual cross-checking of a specific
song before/after an import — no writes, no JSON.
"""

from __future__ import annotations

import sqlite3
import time

from .core import STATS_INDEX_GUID, parse_stats_blob, path_tail

_FIELD_W = 13
_FB_W = 24


def _fmt_ms(ms: int | None) -> str:
    if not ms:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000))


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
        for filename, blob in conn.execute(
            f"SELECT n.filename, d.value FROM metadb_index_{g} n "
            f"JOIN metadb_index_{g}_data d ON n.key = d.key "
            f"WHERE n.filename LIKE '%file://%'"
        ):
            tail = path_tail(filename)
            if tail and query in tail:
                out[tail] = (parse_stats_blob(blob), blob, filename)
    finally:
        conn.close()
    return out


def _gather_fooyin(fooyin_db: str, query: str) -> dict[str, dict]:
    conn = sqlite3.connect(f"file:{fooyin_db}?mode=ro", uri=True)
    out: dict[str, dict] = {}
    try:
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
                    "path": path,
                }
    finally:
        conn.close()
    return out


def _row(field: str, fb: object, fy: object) -> str:
    return f"  {field:<{_FIELD_W}}{str(fb):<{_FB_W}}{fy}"


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
        blocks.append(tail)
        blocks.append(_row("field", "foobar", "fooyin"))
        blocks.append(
            _row("play_count", stats.play_count if stats else "—", y["pc"] if y else "—")
        )
        blocks.append(
            _row(
                "rating",
                _foobar_rating(f[1]) if f else "—",
                _fooyin_rating(y["rating"]) if y else "— (not in fooyin)",
            )
        )
        blocks.append(
            _row(
                "first_played",
                _fmt_ms(stats.first_played_ms) if stats else "—",
                _fmt_ms(y["first"]) if y else "—",
            )
        )
        blocks.append(
            _row(
                "last_played",
                _fmt_ms(stats.last_played_ms) if stats else "—",
                _fmt_ms(y["last"]) if y else "—",
            )
        )
        blocks.append(
            _row(
                "added",
                _fmt_ms(stats.added_ms) if stats else "—",
                _fmt_ms(y["added"]) if y else "—",
            )
        )
        blocks.append(_row("fooyin_hash", "—", y["hash"] if y else "—"))
        blocks.append("")
    if len(tails) > limit:
        blocks.append(f"… {len(tails) - limit} more (raise --limit to see them)")
    return "\n".join(blocks)
