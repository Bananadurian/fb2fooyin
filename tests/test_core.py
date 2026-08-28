import sqlite3
import struct

from fb2fooyin.core import (
    FOOYIN_UNRATED,
    STATS_INDEX_GUID,
    decode_rating,
    detect_stats_guid,
    filetime_to_unix_ms,
    fooyin_rating_to_star,
    fooyin_track_hash,
    norm_track_hash,
    parse_info_tags,
    parse_stats_blob,
    path_tail,
    star_to_fooyin_rating,
    subsong_from_name,
)


def test_filetime_roundtrip_known_value():
    # 2024-ish FILETIME from the real db decodes to a plausible ms value.
    ft = 0x01DAAB3552829C75
    ms = filetime_to_unix_ms(ft)
    assert ms is not None and ms > 1_600_000_000_000  # after 2020


def test_filetime_zero_is_none():
    assert filetime_to_unix_ms(0) is None


def test_decode_rating_anchors():
    assert decode_rating(0x3F) == 1
    assert decode_rating(0x95) == 3
    assert decode_rating(0xBF) == 4
    assert decode_rating(0xEA) == 5
    assert decode_rating(0xFF) is None
    assert decode_rating(0x00) is None


def test_decode_rating_nearest_anchor():
    assert decode_rating(0x40) == 1  # one tick off 0x3F
    assert decode_rating(0xEB) == 5


def test_star_to_fooyin():
    assert star_to_fooyin_rating(5) == 1.0
    assert star_to_fooyin_rating(4) == 0.8


def _blob(play, ft_first, ft_last, ft_added, rating):
    return (
        struct.pack("<I", play)
        + b"\x00\x00\x00\x00"
        + struct.pack("<Q", ft_first)
        + struct.pack("<Q", ft_last)
        + struct.pack("<Q", ft_added)
        + bytes([rating])
        + b"\x00" * 7
    )


def test_parse_stats_blob():
    b = _blob(5, 0x01DAAB3552829C75, 0x01DBAB3552829C75, 0x01DAAB3552829C75, 0xEA)
    s = parse_stats_blob(b)
    assert s is not None
    assert s.play_count == 5
    assert s.rating_star == 5
    assert s.first_played_ms is not None
    assert s.last_played_ms > s.first_played_ms


def test_parse_stats_blob_wrong_length():
    assert parse_stats_blob(b"\x00" * 10) is None


def test_path_tail_windows_and_unix_match():
    fb = "0+file://D:\\11_MusicLib\\11.11_C-Pop\\Faye\\Album\\01. Song.flac"
    fy = "/home/xre/11_music/11.11_c-pop/Faye/Album/01. Song.flac"
    assert path_tail(fb) == path_tail(fy) == "faye/album/01. song.flac"


def test_path_tail_no_genre_returns_none():
    assert path_tail("file:///home/xre/music/random/x.flac") is None


# --- stats index GUID auto-detection -------------------------------------


def _guid_db(tables: dict) -> sqlite3.Connection:
    """In-memory metadb with fake ``metadb_index_<guid>_data`` tables, each
    filled with blobs of the given byte-lengths."""
    conn = sqlite3.connect(":memory:")
    for guid, lengths in tables.items():
        conn.execute(f"CREATE TABLE metadb_index_{guid}_data (key INTEGER, value BLOB)")
        conn.executemany(
            f"INSERT INTO metadb_index_{guid}_data VALUES (?, ?)",
            [(i, b"\x00" * n) for i, n in enumerate(lengths)],
        )
    return conn


def test_detect_stats_guid_picks_uniform_40():
    conn = _guid_db(
        {
            "AAAA_1111": [40, 40, 40],  # stats: uniformly 40 bytes
            "BBBB_2222": [20, 20],  # other index: 20-byte records
            "CCCC_3333": [24, 40, 88],  # history: variable (a 40 exists, not uniform)
        }
    )
    assert detect_stats_guid(conn) == "AAAA_1111"


def test_detect_stats_guid_fallback_when_no_candidate():
    conn = _guid_db({"BBBB_2222": [20, 20], "CCCC_3333": [24, 88]})
    assert detect_stats_guid(conn) == STATS_INDEX_GUID


def test_detect_stats_guid_fallback_when_ambiguous():
    conn = _guid_db({"AAAA_1111": [40, 40], "DDDD_4444": [40]})
    assert detect_stats_guid(conn) == STATS_INDEX_GUID


# --- fooyin TrackHash reproduction (anchored to real fooyin.db values) ----


def test_fooyin_track_hash_multi_artist():
    # XG - UNDEFEATED (ARTIST = XG, VALORANT), verified against the live fooyin.db
    assert (
        fooyin_track_hash(["XG", "VALORANT"], "UNDEFEATED", "1", "1", "UNDEFEATED", 0)
        == "a8dccc8a8b87fd5b71693cef2697e02b"
    )


def test_fooyin_track_hash_single_artist():
    # 方大同 - XZMHXDXH (m4a), verified against the live fooyin.db
    assert (
        fooyin_track_hash(["方大同"], "梦想家 The Dreamer", "1", "1", "XZMHXDXH", 0)
        == "40aed3a2d3ef6c69fbf8b3fbb7b0100b"
    )


def test_fooyin_track_hash_joins_artists_with_comma_no_separator():
    # concatenation is artists.join(",") + album + disc + track + title + subsong, no delimiter
    import hashlib

    expected = hashlib.md5("A,B" "Al" "1" "2" "T" "0".encode()).hexdigest()
    assert fooyin_track_hash(["A", "B"], "Al", "1", "2", "T", 0) == expected


def test_subsong_from_name():
    assert subsong_from_name("0+file://D:\\x.flac") == 0
    assert subsong_from_name("9+file://D:\\cue.flac") == 9
    assert subsong_from_name("file://no-prefix") == 0


# --- foobar metadb.info tag parsing --------------------------------------


def _info(*groups: list[bytes]) -> bytes:
    """Build a metadb.info-style blob: KEY \\0 VALUE... \\0 (empty terminator)."""
    toks: list[bytes] = []
    for g in groups:
        toks.extend(g)
        toks.append(b"")  # group terminator
    return b"\x00".join(toks)


def test_parse_info_flac_uppercase_keys_and_glued_album():
    # ALBUM (alphabetically first) is glued to the binary header with no NUL.
    header = b"\x80\xbf\x8f\x8d"
    blob = _info(
        [header + b"ALBUM", b"My Album"],
        [b"ALBUM ARTIST", b"AA"],
        [b"ARTIST", b"A1", b"A2"],
        [b"DISCNUMBER", b"1"],
        [b"TITLE", b"My Title"],
        [b"TRACKNUMBER", b"3"],
    )
    tags = parse_info_tags(blob)
    assert tags["album"] == "My Album"  # recovered despite gluing
    assert tags["artist"] == ["A1", "A2"]  # ARTIST, not ALBUM ARTIST
    assert tags["disc"] == "1"
    assert tags["track"] == "3"
    assert tags["title"] == "My Title"


def test_parse_info_m4a_lowercase_keys():
    header = b"\x83\x3f"  # ends in '?' before the glued ALBUM
    blob = _info(
        [header + b"ALBUM", b"MP4 Album"],
        [b"album artist", b"AA"],
        [b"artist", b"Solo"],
        [b"discnumber", b"1"],
        [b"title", b"MP4 Title"],
        [b"tracknumber", b"2"],
    )
    tags = parse_info_tags(blob)
    assert tags["album"] == "MP4 Album"
    assert tags["artist"] == ["Solo"]
    assert tags["disc"] == "1"
    assert tags["track"] == "2"
    assert tags["title"] == "MP4 Title"


def test_parse_info_tagless_blob_is_all_empty():
    assert parse_info_tags(b"") == {
        "artist": [],
        "album": "",
        "disc": "",
        "track": "",
        "title": "",
    }


# --- case-folded auxiliary hash -----------------------------------------


def test_norm_hash_ignores_case_and_surrounding_space():
    # The real breakage: an album re-downloaded from another store came back
    # with "A Strange Kind of Love" where foobar had cached "...Kind Of Love".
    a = norm_track_hash(["Diane Birch"], "The Velveteen Age", "1", "1", "A Strange Kind Of Love", 0)
    b = norm_track_hash(["diane birch"], "the velveteen age", "1", "1", " a strange kind of love ", 0)
    assert a == b


def test_norm_hash_still_separates_different_recordings():
    a = norm_track_hash(["A"], "Al", "1", "1", "Song", 0)
    assert a != norm_track_hash(["A"], "Al", "1", "2", "Song", 0)
    assert a != norm_track_hash(["A"], "Al", "1", "1", "Other", 0)
    assert a != norm_track_hash(["B"], "Al", "1", "1", "Song", 0)


def test_norm_hash_differs_from_exact_hash_when_case_differs():
    # Otherwise the extra layer would be redundant with the exact hash.
    args = (["A"], "Al", "1", "1", "Song", 0)
    assert norm_track_hash(*args) != fooyin_track_hash(*args)


def test_norm_hash_matches_exact_hash_for_already_folded_tags():
    args = (["a"], "al", "1", "1", "song", 0)
    assert norm_track_hash(*args) == fooyin_track_hash(*args)


# --- fooyin rating round trip -------------------------------------------


def test_fooyin_rating_to_star_round_trips_every_observed_value():
    for star in range(1, 6):
        assert fooyin_rating_to_star(star_to_fooyin_rating(star)) == star


def test_fooyin_rating_handles_unrated_and_float32_noise():
    assert fooyin_rating_to_star(FOOYIN_UNRATED) is None
    assert fooyin_rating_to_star(0.0) is None
    assert fooyin_rating_to_star(None) is None
    # values as stored by fooyin (float32 widened to double)
    assert fooyin_rating_to_star(0.40000000596046448) == 2
    assert fooyin_rating_to_star(0.80000001192092896) == 4
