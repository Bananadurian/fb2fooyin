import sqlite3

from fb2fooyin.importer import _SIDECAR_DDL, plan_changes


def _fooyin_conn():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE Tracks (FilePath TEXT, TrackHash TEXT);
        CREATE TABLE TrackStats (
            TrackHash TEXT PRIMARY KEY, LastSeen INTEGER, AddedDate INTEGER,
            FirstPlayed INTEGER, LastPlayed INTEGER, PlayCount INTEGER DEFAULT 0,
            Rating REAL DEFAULT 0
        );
        """
    )
    conn.execute(_SIDECAR_DDL)
    return conn


def _record(tail, pc, first=1000, last=2000, added=500, star=5):
    return {
        "tail": tail,
        "play_count": pc,
        "first_played_ms": first,
        "last_played_ms": last,
        "added_ms": added,
        "rating_star": star,
    }


def _apply_plan_in_memory(conn, records):
    """Mimic apply_changes against the in-memory db (no backup/lock)."""
    changes, unmatched = plan_changes(conn, records)
    now = 12345
    for ch in changes:
        added, first, last, pc, rating = ch.new
        conn.execute(
            "INSERT INTO TrackStats (TrackHash, AddedDate, FirstPlayed, LastPlayed, PlayCount, Rating)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(TrackHash) DO UPDATE SET AddedDate=excluded.AddedDate,"
            " FirstPlayed=excluded.FirstPlayed, LastPlayed=excluded.LastPlayed,"
            " PlayCount=excluded.PlayCount, Rating=excluded.Rating",
            (ch.track_hash, added, first, last, pc, rating),
        )
        conn.execute(
            "INSERT INTO _fb2fooyin_import (TrackHash, ContributedPlayCount, ImportedAt)"
            " VALUES (?,?,?)"
            " ON CONFLICT(TrackHash) DO UPDATE SET ContributedPlayCount=excluded.ContributedPlayCount,"
            " ImportedAt=excluded.ImportedAt",
            (ch.track_hash, ch.contributed, now),
        )
    conn.commit()
    return changes, unmatched


def test_insert_new_row():
    conn = _fooyin_conn()
    conn.execute("INSERT INTO Tracks VALUES ('/x/11.11_c-pop/a/al/01. s.flac', 'H1')")
    _apply_plan_in_memory(conn, [_record("a/al/01. s.flac", 3)])
    row = conn.execute("SELECT PlayCount, Rating FROM TrackStats WHERE TrackHash='H1'").fetchone()
    assert row == (3, 1.0)


def test_playcount_idempotent_across_reruns():
    conn = _fooyin_conn()
    conn.execute("INSERT INTO Tracks VALUES ('/x/11.11_c-pop/a/al/01. s.flac', 'H1')")
    rec = [_record("a/al/01. s.flac", 7)]
    _apply_plan_in_memory(conn, rec)
    _apply_plan_in_memory(conn, rec)  # rerun must not double-count
    pc = conn.execute("SELECT PlayCount FROM TrackStats WHERE TrackHash='H1'").fetchone()[0]
    assert pc == 7


def test_fooyin_own_plays_survive_rerun():
    conn = _fooyin_conn()
    conn.execute("INSERT INTO Tracks VALUES ('/x/11.11_c-pop/a/al/01. s.flac', 'H1')")
    rec = [_record("a/al/01. s.flac", 5)]
    _apply_plan_in_memory(conn, rec)
    # user plays it twice inside fooyin between runs
    conn.execute("UPDATE TrackStats SET PlayCount = PlayCount + 2 WHERE TrackHash='H1'")
    conn.commit()
    _apply_plan_in_memory(conn, rec)
    pc = conn.execute("SELECT PlayCount FROM TrackStats WHERE TrackHash='H1'").fetchone()[0]
    assert pc == 7  # 5 from foobar + 2 fooyin's own, not re-added


def test_growing_foobar_count_adds_delta():
    conn = _fooyin_conn()
    conn.execute("INSERT INTO Tracks VALUES ('/x/11.11_c-pop/a/al/01. s.flac', 'H1')")
    _apply_plan_in_memory(conn, [_record("a/al/01. s.flac", 5)])
    _apply_plan_in_memory(conn, [_record("a/al/01. s.flac", 8)])  # +3 in foobar
    pc = conn.execute("SELECT PlayCount FROM TrackStats WHERE TrackHash='H1'").fetchone()[0]
    assert pc == 8


def test_added_and_first_take_earlier():
    conn = _fooyin_conn()
    conn.execute("INSERT INTO Tracks VALUES ('/x/11.11_c-pop/a/al/01. s.flac', 'H1')")
    # fooyin already scanned it with a late AddedDate and no plays
    conn.execute(
        "INSERT INTO TrackStats (TrackHash, AddedDate, FirstPlayed, LastPlayed, PlayCount, Rating)"
        " VALUES ('H1', 9999, 0, 0, 0, -1.0)"
    )
    conn.commit()
    _apply_plan_in_memory(conn, [_record("a/al/01. s.flac", 1, first=1000, added=500)])
    added, first = conn.execute(
        "SELECT AddedDate, FirstPlayed FROM TrackStats WHERE TrackHash='H1'"
    ).fetchone()
    assert added == 500  # earlier foobar value wins
    assert first == 1000


def test_keep_fooyin_rating_flag():
    conn = _fooyin_conn()
    conn.execute("INSERT INTO Tracks VALUES ('/x/11.11_c-pop/a/al/01. s.flac', 'H1')")
    # fooyin already has a 3-star rating the user set
    conn.execute(
        "INSERT INTO TrackStats (TrackHash, AddedDate, FirstPlayed, LastPlayed, PlayCount, Rating)"
        " VALUES ('H1', 500, 0, 0, 0, 0.6)"
    )
    conn.commit()
    rec = [_record("a/al/01. s.flac", 1, star=5)]  # foobar says 5 stars

    # default: foobar wins
    changes, _ = plan_changes(conn, rec)
    assert changes[0].new[4] == 1.0

    # with the flag: fooyin's 3 stars is preserved
    changes, _ = plan_changes(conn, rec, keep_fooyin_rating=True)
    assert changes[0].new[4] == 0.6


def test_keep_fooyin_rating_still_fills_empty():
    conn = _fooyin_conn()
    conn.execute("INSERT INTO Tracks VALUES ('/x/11.11_c-pop/a/al/01. s.flac', 'H1')")
    # fooyin unrated (-1.0); foobar has 4 stars -> flag still fills it
    conn.execute(
        "INSERT INTO TrackStats (TrackHash, AddedDate, FirstPlayed, LastPlayed, PlayCount, Rating)"
        " VALUES ('H1', 500, 0, 0, 0, -1.0)"
    )
    conn.commit()
    changes, _ = plan_changes(conn, [_record("a/al/01. s.flac", 1, star=4)], keep_fooyin_rating=True)
    assert changes[0].new[4] == 0.8


def test_unmatched_tail_reported():
    conn = _fooyin_conn()
    changes, unmatched = plan_changes(conn, [_record("nope/x/01. y.flac", 1)])
    assert changes == []
    assert unmatched == ["nope/x/01. y.flac"]


def test_duplicate_copies_share_one_hash_sum_and_idempotent():
    # Two physical copies of the same recording -> two tails, ONE TrackHash.
    conn = _fooyin_conn()
    conn.execute("INSERT INTO Tracks VALUES ('/x/11.11_c-pop/a/copy1/01. s.flac', 'H1')")
    conn.execute("INSERT INTO Tracks VALUES ('/x/11.20_rnb/a/copy2/01. s.flac', 'H1')")
    recs = [
        _record("a/copy1/01. s.flac", 4, first=1000, last=5000, added=500, star=None),
        _record("a/copy2/01. s.flac", 3, first=800, last=9000, added=400, star=4),
    ]
    changes, _ = _apply_plan_in_memory(conn, recs)
    # one row written, not two racing writes
    assert len(changes) == 1
    row = conn.execute(
        "SELECT PlayCount, FirstPlayed, LastPlayed, AddedDate, Rating FROM TrackStats WHERE TrackHash='H1'"
    ).fetchone()
    assert row == (7, 800, 9000, 400, 0.8)  # 4+3, earliest, latest, earliest, 4★
    # rerun must not double-count or flip-flop
    _apply_plan_in_memory(conn, recs)
    pc = conn.execute("SELECT PlayCount FROM TrackStats WHERE TrackHash='H1'").fetchone()[0]
    assert pc == 7
