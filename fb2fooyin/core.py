"""Shared codecs and key normalization for the foobar2000 -> fooyin migration.

All the fiddly, verified-against-real-data logic lives here so both the
``export`` and ``import`` stages agree on it, and so it can be unit tested
without touching either database.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass

# --- Windows FILETIME <-> Unix time --------------------------------------

# 100-nanosecond ticks between 1601-01-01 (FILETIME epoch) and 1970-01-01.
_FILETIME_EPOCH_DIFF = 116_444_736_000_000_000
# FILETIME is 100 ns ticks; fooyin stores Unix milliseconds -> divide by 10_000.
_TICKS_PER_MS = 10_000


def filetime_to_unix_ms(ft: int) -> int | None:
    """Convert a Windows FILETIME to Unix milliseconds.

    Returns ``None`` for a zero/invalid FILETIME, which foobar2000 uses to
    mean "never" (e.g. an added-but-never-played track).
    """
    if ft <= _FILETIME_EPOCH_DIFF:
        return None
    return (ft - _FILETIME_EPOCH_DIFF) // _TICKS_PER_MS


# --- Rating -------------------------------------------------------------

# foobar2000's foo_playcount stores the star rating as a single byte using a
# roughly linear encoding (~42.5 apart). These anchors were read straight out
# of the real metadb; 2 stars was not present in the data and is interpolated.
# 0xFF (and 0x00) mean "unrated".
_RATING_ANCHORS: dict[int, int] = {
    0x3F: 1,  # 63
    0x6A: 2,  # 106  (interpolated, unobserved)
    0x95: 3,  # 149
    0xBF: 4,  # 191
    0xEA: 5,  # 234
}
_UNRATED_BYTES = {0x00, 0xFF}


def decode_rating(byte: int) -> int | None:
    """Decode a foobar rating byte to a 1-5 star value, or ``None`` if unrated.

    Uses nearest-anchor matching so the decode is robust even if a byte lands a
    tick off the exact anchor (rounding in foobar's own encoder).
    """
    if byte in _UNRATED_BYTES:
        return None
    nearest = min(_RATING_ANCHORS, key=lambda anchor: abs(anchor - byte))
    return _RATING_ANCHORS[nearest]


def star_to_fooyin_rating(star: int) -> float:
    """fooyin stores rating as a 0.0-1.0 REAL (star / 5)."""
    return star / 5.0


# fooyin's sentinel for "no rating" (observed in TrackStats.Rating).
FOOYIN_UNRATED = -1.0


# --- foobar stats BLOB --------------------------------------------------

STATS_INDEX_GUID = "C653739F_14B3_4EF2_819B_A3E2883230AE"
_STATS_BLOB_LEN = 40


@dataclass(frozen=True)
class Stats:
    play_count: int
    first_played_ms: int | None
    last_played_ms: int | None
    added_ms: int | None
    rating_star: int | None


def parse_stats_blob(blob: bytes) -> Stats | None:
    """Parse a 40-byte foo_playcount stats BLOB.

    Layout (little-endian), verified against the live metadb:
        [0:4]   uint32  play count
        [4:8]   (zero)
        [8:16]  FILETIME first played
        [16:24] FILETIME last played
        [24:32] FILETIME added
        [32]    rating byte (0xFF = unrated)
        [33:40] (zero)
    """
    if len(blob) != _STATS_BLOB_LEN:
        return None
    play_count = struct.unpack_from("<I", blob, 0)[0]
    ft_first = struct.unpack_from("<Q", blob, 8)[0]
    ft_last = struct.unpack_from("<Q", blob, 16)[0]
    ft_added = struct.unpack_from("<Q", blob, 24)[0]
    rating_byte = blob[32]
    return Stats(
        play_count=play_count,
        first_played_ms=filetime_to_unix_ms(ft_first),
        last_played_ms=filetime_to_unix_ms(ft_last),
        added_ms=filetime_to_unix_ms(ft_added),
        rating_star=decode_rating(rating_byte),
    )


# --- Matching key: album-relative path tail -----------------------------

# Everything up to and including the genre folder (``11.NN_...``) differs
# between the old Windows library and the current Linux one (root, separators,
# case). Everything below it (artist/album/file) is byte-identical, so the tail
# is the stable join key. Matched 99.9% of fooyin tracks in testing.
_GENRE_TAIL_RE = re.compile(r"/11\.\d\d[^/]*/(.*)$")
_SUBSONG_PREFIX_RE = re.compile(r"^\d+\+")


def path_tail(path: str) -> str | None:
    """Normalize a foobar or fooyin path to its case-folded album-relative tail.

    Returns ``None`` for paths that carry no genre folder (radio streams,
    zip-embedded tracks, etc.) which therefore cannot be matched.
    """
    p = _SUBSONG_PREFIX_RE.sub("", path)  # strip foobar "0+" subsong prefix
    p = p.replace("file://", "")
    p = p.replace("\\", "/").lower()
    m = _GENRE_TAIL_RE.search(p)
    return m.group(1) if m else None
