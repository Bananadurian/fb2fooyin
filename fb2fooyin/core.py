"""Shared codecs and key normalization for the foobar2000 -> fooyin migration.

All the fiddly, verified-against-real-data logic lives here so both the
``export`` and ``import`` stages agree on it, and so it can be unit tested
without touching either database.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import struct
from dataclasses import dataclass

# Version of the intermediate JSON payload, shared by both producers (the
# foobar ``export`` and the fooyin ``snapshot``) so consumers can tell what
# fields to expect. v5 added ``hash_norm`` and the ``kind`` producer tag.
SCHEMA_VERSION = 5

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


def fooyin_rating_to_star(rating: float | None) -> int | None:
    """Inverse of ``star_to_fooyin_rating``; ``None`` when unrated.

    Every rating observed in the live database is an exact multiple of 0.2
    (plus float32 storage noise like 0.40000000596), so rounding to the
    nearest star is lossless in practice. 0.0 is treated as unrated, matching
    foobar's ``_UNRATED_BYTES``.
    """
    if rating is None or rating <= 0:
        return None
    return max(1, min(5, round(rating * 5)))


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


def detect_stats_guid(conn: sqlite3.Connection) -> str:
    """Find the Playback Statistics index GUID in an open foobar metadb.

    foobar registers several component indexes as ``metadb_index_<GUID>_data``
    payload tables; only the Playback Statistics one stores the fixed 40-byte
    stats BLOB (see ``parse_stats_blob``). We pick the non-empty payload table
    whose blobs are *uniformly* 40 bytes and parse as a valid stats record —
    the variable-length history index also holds some 40-byte rows, so "has a
    40-byte blob" is not enough; "all blobs are 40 bytes" is unique.

    Returns the underscore-form GUID used in the real SQLite table names. Falls
    back to the well-known ``STATS_INDEX_GUID`` when detection is inconclusive
    (no candidate, or more than one).
    """
    prefix, suffix = "metadb_index_", "_data"
    candidates: list[str] = []
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'metadb_index_%'"
        )
        if r[0].endswith(suffix)
    ]
    for t in tables:
        n, lo, hi = conn.execute(
            f"SELECT COUNT(*), MIN(length(value)), MAX(length(value)) FROM {t}"
        ).fetchone()
        if not n or lo != _STATS_BLOB_LEN or hi != _STATS_BLOB_LEN:
            continue
        sample = conn.execute(f"SELECT value FROM {t} LIMIT 1").fetchone()
        if sample and parse_stats_blob(sample[0]) is not None:
            candidates.append(t[len(prefix) : -len(suffix)])
    return candidates[0] if len(candidates) == 1 else STATS_INDEX_GUID


# --- fooyin TrackHash reproduction --------------------------------------

# fooyin identifies a recording by a content hash of its tags, not its path,
# so the hash survives files being moved or renamed. Reproducing it lets us
# match foobar stats to fooyin tracks by identity instead of by path.
#
# Verified against fooyin's source (src/core/track.cpp Track::generateHash +
# include/utils/crypto.h Utils::generateHash): the hash is the lower-case hex
# MD5 of the UTF-8 concatenation (NO separator) of, in order:
#     artists joined by ","  ++  album  ++  discNumber  ++  trackNumber
#     ++  title  ++  str(subsong)
# using the raw tag strings (no case folding). Reproduced 100% against a live
# fooyin.db.


def fooyin_track_hash(
    artists: list[str],
    album: str,
    disc: str,
    track: str,
    title: str,
    subsong: int,
) -> str:
    """Recompute fooyin's content-based TrackHash from tag fields.

    ``disc``/``track``/``album``/``title`` are the raw tag strings ("" if
    absent); ``artists`` is the ordered list of ARTIST values.

    Note: fooyin falls back to ``directory + filename`` when the title is empty.
    We cannot reproduce that from foobar's Windows paths, so a title-less track
    will hash-miss and fall back to path-tail matching.
    """
    payload = ",".join(artists) + album + disc + track + title + str(subsong)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


# Tags drift between the two libraries even when the recording is identical:
# re-downloading an album from another store, or re-tagging it, commonly only
# changes letter case ("A Strange Kind Of Love" -> "A Strange Kind of Love").
# fooyin's hash is over the raw strings, so such a track hash-misses. A second,
# case-folded hash recovers it. Deliberately minimal — only ``strip().lower()``:
# folding more (inner whitespace, Unicode forms) rescued no extra track on the
# real library while widening the chance two genuinely different recordings
# collapse onto one hash, which would silently write the wrong play counts.


def norm_tag(s: str) -> str:
    """Fold a tag string for the auxiliary hash: trim, then lower-case."""
    return s.strip().lower()


def norm_track_hash(
    artists: list[str],
    album: str,
    disc: str,
    track: str,
    title: str,
    subsong: int,
) -> str:
    """``fooyin_track_hash`` over case-folded tags — the auxiliary match key.

    Same payload layout as the real hash, so it stays in lock-step with it;
    only the strings are normalized. Not a fooyin concept: both sides of a
    comparison must recompute it.
    """
    payload = (
        ",".join(norm_tag(a) for a in artists)
        + norm_tag(album)
        + norm_tag(disc)
        + norm_tag(track)
        + norm_tag(title)
        + str(subsong)
    )
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def subsong_from_name(name: str) -> int:
    """foobar metadb keys are ``<subsong>+<uri>`` (0 for normal single-song files)."""
    head = name.split("+", 1)[0]
    return int(head) if head.isdigit() else 0


# --- foobar metadb.info tag BLOB ----------------------------------------

# The per-file ``metadb.info`` BLOB stores tags as NUL-delimited tokens laid out
# as ``KEY \0 VALUE [\0 VALUE ...] \0`` groups after a binary header. Two quirks,
# both verified against the live metadb across formats:
#   * Key case follows the source format's tag names — FLAC/Vorbis are
#     upper-case (TITLE, TRACKNUMBER), MP4/m4a are lower-case (title,
#     tracknumber). So keys are matched case-insensitively.
#   * The alphabetically-first tag (always "ALBUM") is glued onto the end of the
#     binary header with no NUL before it, so it never appears as a clean token.
#     It is recovered via its unique "ALBUM" suffix (nothing else ends in it —
#     "ALBUM ARTIST"/"ALBUMARTISTSORT" do not).


def _decode(tok: bytes) -> str:
    try:
        return tok.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def _collect_values(tokens: list[bytes], start: int) -> list[str]:
    """Value tokens following a key, up to the empty-token group terminator."""
    vals: list[str] = []
    j = start
    while j < len(tokens) and tokens[j] != b"":
        vals.append(_decode(tokens[j]))
        j += 1
    return vals


# The tag shape every stage agrees on; also the "no info blob" default that
# export/inspect reuse (they import ``EMPTY_TAGS`` instead of redefining it).
EMPTY_TAGS = {"artist": [], "album": "", "disc": "", "track": "", "title": ""}


def parse_info_tags(blob: bytes) -> dict:
    """Extract the tag fields fooyin hashes from a foobar ``metadb.info`` BLOB.

    Returns ``{"artist": list[str], "album": str, "disc": str, "track": str,
    "title": str}`` with "" / [] for anything absent (e.g. tag-less WAV rips).
    """
    tokens = bytes(blob).split(b"\x00")
    out: dict = dict(EMPTY_TAGS)
    for i, tok in enumerate(tokens):
        # ALBUM first: unique suffix, survives being glued to the header binary.
        if not out["album"] and tok.upper().endswith(b"ALBUM") and i + 1 < len(tokens):
            out["album"] = _decode(tokens[i + 1])
        try:
            key = tok.decode("utf-8").upper()
        except UnicodeDecodeError:
            continue
        if key == "ARTIST" and not out["artist"]:
            out["artist"] = _collect_values(tokens, i + 1)
        elif key == "TITLE" and not out["title"]:
            vals = _collect_values(tokens, i + 1)
            out["title"] = vals[0] if vals else ""
        elif key == "DISCNUMBER" and not out["disc"]:
            vals = _collect_values(tokens, i + 1)
            out["disc"] = vals[0] if vals else ""
        elif key == "TRACKNUMBER" and not out["track"]:
            vals = _collect_values(tokens, i + 1)
            out["track"] = vals[0] if vals else ""
    return out


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


# --- fooyin database reads ----------------------------------------------

# Boundary note: the tool reads fooyin's tag columns ONLY to recompute hashes
# (``norm_track_hash``, and to prove the real ``TrackHash`` is reproducible).
# It never compares tag *values* between the two libraries — matching stays a
# hash/path equality test, never a fuzzy field comparison. Verified: recomputing
# ``fooyin_track_hash`` from these columns reproduces 9802/9802 stored hashes.

FOOYIN_ARTIST_SEP = "\x1f"  # fooyin joins Tracks.Artists with a unit separator


@dataclass(frozen=True)
class FooyinTrack:
    track_hash: str
    file_path: str
    artists: list[str]
    album: str
    disc: str
    track: str
    title: str
    subsong: int

    @property
    def norm_hash(self) -> str:
        return norm_track_hash(
            self.artists, self.album, self.disc, self.track, self.title, self.subsong
        )


def read_fooyin_tracks(conn: sqlite3.Connection) -> list[FooyinTrack]:
    """Every fooyin track that carries a content hash."""
    rows = conn.execute(
        "SELECT TrackHash, FilePath, Artists, Album, DiscNumber, TrackNumber, "
        "Title, Subsong FROM Tracks WHERE TrackHash IS NOT NULL"
    )
    return [
        FooyinTrack(
            track_hash=h,
            file_path=path,
            artists=artists.split(FOOYIN_ARTIST_SEP) if artists else [],
            album=album or "",
            disc=disc or "",
            track=track or "",
            title=title or "",
            subsong=subsong or 0,
        )
        for h, path, artists, album, disc, track, title, subsong in rows
    ]


def build_norm_index(tracks: list[FooyinTrack]) -> dict[str, list[str]]:
    """Map case-folded hash -> distinct TrackHash (may be several: see §5).

    Collisions are rare and benign on the real library (3 of 9731, all the same
    recording present in two album editions), which is the existing
    "duplicate copies share one row" case rather than a new failure mode.
    """
    index: dict[str, list[str]] = {}
    for t in tracks:
        bucket = index.setdefault(t.norm_hash, [])
        if t.track_hash not in bucket:
            bucket.append(t.track_hash)
    return index


# The sidecar table this tool owns inside fooyin.db: how much play count each
# import run contributed, so a re-run can back its own previous contribution
# out instead of double-counting (see importer.merge_one).
SIDECAR_TABLE = "_fb2fooyin_import"

SIDECAR_DDL = f"""
CREATE TABLE IF NOT EXISTS {SIDECAR_TABLE} (
    TrackHash TEXT PRIMARY KEY,
    ContributedPlayCount INTEGER NOT NULL,
    ImportedAt INTEGER NOT NULL
)
"""


# --- merge helpers (0/None == "unknown") --------------------------------


def min_pos(*vals: int | None) -> int | None:
    """Smallest value that is neither None nor 0."""
    present = [v for v in vals if v]
    return min(present) if present else None


def max_pos(*vals: int | None) -> int | None:
    """Largest value that is neither None nor 0."""
    present = [v for v in vals if v]
    return max(present) if present else None


def read_trackstats(conn: sqlite3.Connection) -> dict[str, tuple]:
    """hash -> (AddedDate, FirstPlayed, LastPlayed, PlayCount, Rating)."""
    return {
        h: (added, first, last, pc, rating)
        for h, added, first, last, pc, rating in conn.execute(
            "SELECT TrackHash, AddedDate, FirstPlayed, LastPlayed, PlayCount, Rating "
            "FROM TrackStats"
        )
    }


def read_contributions(conn: sqlite3.Connection) -> dict[str, int]:
    """hash -> play count this tool previously wrote; empty if never imported."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (SIDECAR_TABLE,)
    ).fetchone()
    if not exists:
        return {}
    return dict(
        conn.execute(f"SELECT TrackHash, ContributedPlayCount FROM {SIDECAR_TABLE}")
    )
