import struct

from fb2fooyin.core import (
    decode_rating,
    filetime_to_unix_ms,
    parse_stats_blob,
    path_tail,
    star_to_fooyin_rating,
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
