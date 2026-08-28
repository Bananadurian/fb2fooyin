import sqlite3

import pytest

from fb2fooyin.core import FOOYIN_UNRATED, SIDECAR_DDL, norm_track_hash
from fb2fooyin.restore import (
    apply_restore,
    list_orphans,
    merge_restore,
    missing_library_roots,
    plan_restore,
)


def _fooyin_conn():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE Tracks (
            FilePath TEXT, TrackHash TEXT, Title TEXT, Artists TEXT, Album TEXT,
            DiscNumber TEXT, TrackNumber TEXT, Subsong INTEGER DEFAULT 0
        );
        CREATE TABLE TrackStats (
            TrackHash TEXT PRIMARY KEY, AddedDate INTEGER, FirstPlayed INTEGER,
            LastPlayed INTEGER, PlayCount INTEGER DEFAULT 0, Rating REAL DEFAULT -1.0
        );
        CREATE TABLE Libraries (LibraryID INTEGER, Name TEXT, Path TEXT);
        """
    )
    conn.execute(SIDECAR_DDL)
    return conn


def _add_track(conn, path, h, title="Song", artists="A", album="Al", disc="1", track="1"):
    conn.execute(
        "INSERT INTO Tracks (FilePath, TrackHash, Title, Artists, Album,"
        " DiscNumber, TrackNumber, Subsong) VALUES (?,?,?,?,?,?,?,0)",
        (path, h, title, artists, album, disc, track),
    )


def _add_stats(conn, h, pc, added=500, first=1000, last=2000, rating=FOOYIN_UNRATED):
    conn.execute(
        "INSERT INTO TrackStats (TrackHash, AddedDate, FirstPlayed, LastPlayed,"
        " PlayCount, Rating) VALUES (?,?,?,?,?,?)",
        (h, added, first, last, pc, rating),
    )


def _snap(hash, hash_norm, path, pc, title="Song", track="1", contributed=None, star=None):
    return {
        "hash": hash,
        "hash_norm": hash_norm,
        "file_path": path,
        "track_number": track,
        "title": title,
        "play_count": pc,
        "first_played_ms": 1000,
        "last_played_ms": 2000,
        "added_ms": 500,
        "rating_star": star,
        "contributed": contributed,
    }


# --- resolution layers --------------------------------------------------


def test_intact_identity_is_skipped():
    # The hash is still in Tracks: nothing broke, so restore must write nothing.
    conn = _fooyin_conn()
    _add_track(conn, "/x/al/01.flac", "H1")
    _add_stats(conn, "H1", 10)
    changes, unresolved, report = plan_restore(conn, [_snap("H1", "N1", "/x/al/01.flac", 10)])
    assert changes == [] and unresolved == []
    assert (report.intact, report.by_norm) == (1, 0)


def test_norm_hash_moves_stats_to_the_retagged_track():
    # Album re-downloaded from another store: folder renamed AND title case
    # rewritten, so the exact hash and the path both miss. Only the folded hash
    # still lines up — this is the Diane Birch failure in miniature.
    conn = _fooyin_conn()
    _add_track(conn, "/x/al_amazon/01-Song.flac", "H2", title="A Strange Kind of Love")
    _add_stats(conn, "H2", 0)
    stale = norm_track_hash(["A"], "Al", "1", "1", "A Strange Kind Of Love", 0)
    changes, _, report = plan_restore(
        conn, [_snap("H1", stale, "/x/al_web/01. Song.flac", 42)]
    )
    assert report.by_norm == 1
    assert changes[0].target_hash == "H2" and changes[0].source == "norm"
    assert changes[0].new[3] == 42


def test_path_layer_catches_retag_in_place():
    # Tags rewritten without renaming anything: the hash changed, the path did not.
    conn = _fooyin_conn()
    _add_track(conn, "/x/al/01.flac", "H2", title="Song (Remastered)")
    _add_stats(conn, "H2", 3)
    changes, _, report = plan_restore(conn, [_snap("H1", "NOPE", "/x/al/01.flac", 40)])
    assert report.by_path == 1
    assert changes[0].target_hash == "H2" and changes[0].source == "path"
    assert changes[0].new[3] == 40


def test_fuzzy_needs_to_scope():
    # Without --to the heuristic layer stays off: track 1 + "song" would collide
    # across the whole library.
    conn = _fooyin_conn()
    _add_track(conn, "/x/new/01.flac", "H2", title="Song", track="1")
    _add_stats(conn, "H2", 0)
    rec = [_snap("H1", "NOPE", "/x/old/01.flac", 9, title="Song", track="1")]
    _changes, unresolved, report = plan_restore(conn, rec)
    assert report.fuzzy_available is False
    assert report.unresolved == 1 and unresolved == ["/x/old/01.flac"]


def test_fuzzy_matches_within_to_scope():
    conn = _fooyin_conn()
    _add_track(conn, "/x/new/01.flac", "H2", title="SONG", track="1")
    _add_stats(conn, "H2", 0)
    rec = [_snap("H1", "NOPE", "/x/old/01.flac", 9, title="song", track="1")]
    changes, _, report = plan_restore(conn, rec, to_prefix="/x/new")
    assert report.by_fuzzy == 1
    assert changes[0].target_hash == "H2" and changes[0].source == "fuzzy"


def test_fuzzy_disabled_above_limit():
    conn = _fooyin_conn()
    _add_track(conn, "/x/new/01.flac", "H2", title="Song", track="1")
    _add_stats(conn, "H2", 0)
    recs = [_snap(f"H{i}", "NOPE", f"/x/old/{i}.flac", 1) for i in range(5)]
    _changes, _unresolved, report = plan_restore(conn, recs, to_prefix="/x/new", fuzzy_limit=3)
    assert report.fuzzy_available is False


# --- merge semantics ----------------------------------------------------


def test_playcount_takes_max_not_sum():
    # Additive would double-count on a second run; restore is a rescue command
    # that gets run twice more often than not.
    assert merge_restore((None, None, None, 3, FOOYIN_UNRATED), (500, 1000, 2000, 40, None))[3] == 40
    assert merge_restore((None, None, None, 90, FOOYIN_UNRATED), (500, 1000, 2000, 40, None))[3] == 90


def test_restore_is_idempotent():
    conn = _fooyin_conn()
    _add_track(conn, "/x/al/01.flac", "H2")
    _add_stats(conn, "H2", 0)
    rec = [_snap("H1", "NOPE", "/x/al/01.flac", 40)]
    changes, _, _ = plan_restore(conn, rec)
    _add_stats_update(conn, changes)
    changes2, _, _ = plan_restore(conn, rec)
    assert changes2 == []


def _add_stats_update(conn, changes):
    for ch in changes:
        added, first, last, pc, rating = ch.new
        conn.execute(
            "INSERT INTO TrackStats (TrackHash, AddedDate, FirstPlayed, LastPlayed,"
            " PlayCount, Rating) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(TrackHash) DO UPDATE SET AddedDate=excluded.AddedDate,"
            " FirstPlayed=excluded.FirstPlayed, LastPlayed=excluded.LastPlayed,"
            " PlayCount=excluded.PlayCount, Rating=excluded.Rating",
            (ch.target_hash, added, first, last, pc, rating),
        )
    conn.commit()


def test_existing_fooyin_rating_is_never_overwritten():
    old = (500, 1000, 2000, 5, 0.8)
    assert merge_restore(old, (500, 1000, 2000, 5, 0.2))[4] == 0.8
    unrated = (500, 1000, 2000, 5, FOOYIN_UNRATED)
    assert merge_restore(unrated, (500, 1000, 2000, 5, 0.2))[4] == 0.2


# --- write path ---------------------------------------------------------


def _write_db(tmp_path):
    path = tmp_path / "fooyin.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE Tracks (
            FilePath TEXT, TrackHash TEXT, Title TEXT, Artists TEXT, Album TEXT,
            DiscNumber TEXT, TrackNumber TEXT, Subsong INTEGER DEFAULT 0
        );
        CREATE TABLE TrackStats (
            TrackHash TEXT PRIMARY KEY, AddedDate INTEGER, FirstPlayed INTEGER,
            LastPlayed INTEGER, PlayCount INTEGER DEFAULT 0, Rating REAL DEFAULT -1.0
        );
        CREATE TABLE Libraries (LibraryID INTEGER, Name TEXT, Path TEXT);
        """
    )
    conn.execute(SIDECAR_DDL)
    return path, conn


def test_sidecar_is_carried_across(tmp_path):
    # Without this, a later foobar import sees no prior contribution on the new
    # hash and adds its own count on top of the restored one.
    path, conn = _write_db(tmp_path)
    _add_track(conn, "/x/al/01.flac", "H2")
    _add_stats(conn, "H2", 0)
    conn.commit()
    conn.close()
    changes, _, _ = plan_restore(
        sqlite3.connect(path), [_snap("H1", "NOPE", "/x/al/01.flac", 40, contributed=38)]
    )
    apply_restore(str(path), changes)
    conn = sqlite3.connect(path)
    assert conn.execute(
        "SELECT ContributedPlayCount FROM _fb2fooyin_import WHERE TrackHash='H2'"
    ).fetchone() == (38,)


def test_prune_moved_skips_fuzzy_matches(tmp_path):
    path, conn = _write_db(tmp_path)
    _add_track(conn, "/x/new/01.flac", "H2", title="Song", track="1")
    _add_stats(conn, "H2", 0)
    _add_stats(conn, "H1", 40)  # the orphan
    conn.commit()
    conn.close()
    rec = [_snap("H1", "NOPE", "/x/old/01.flac", 40, title="Song", track="1")]
    changes, _, _ = plan_restore(sqlite3.connect(path), rec, to_prefix="/x/new")
    assert changes[0].source == "fuzzy"
    apply_restore(str(path), changes, prune_moved=True)
    conn = sqlite3.connect(path)
    # heuristic match: the source row survives so it can still be reviewed
    assert conn.execute("SELECT COUNT(*) FROM TrackStats WHERE TrackHash='H1'").fetchone() == (1,)


def test_prune_moved_removes_deterministic_source(tmp_path):
    path, conn = _write_db(tmp_path)
    _add_track(conn, "/x/al/01.flac", "H2")
    _add_stats(conn, "H2", 0)
    _add_stats(conn, "H1", 40)
    conn.commit()
    conn.close()
    rec = [_snap("H1", "NOPE", "/x/al/01.flac", 40)]
    changes, _, _ = plan_restore(sqlite3.connect(path), rec)
    assert changes[0].source == "path"
    apply_restore(str(path), changes, prune_moved=True)
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM TrackStats WHERE TrackHash='H1'").fetchone() == (0,)


# --- orphans ------------------------------------------------------------


def test_list_orphans_hides_empty_rows_by_default():
    conn = _fooyin_conn()
    _add_track(conn, "/x/al/01.flac", "H1")
    _add_stats(conn, "H1", 5)
    _add_stats(conn, "GONE", 7)
    _add_stats(conn, "EMPTY", 0)
    assert [o.track_hash for o in list_orphans(conn)] == ["GONE"]
    assert {o.track_hash for o in list_orphans(conn, include_empty=True)} == {"GONE", "EMPTY"}


def test_missing_library_roots_flags_unmounted_paths(tmp_path):
    conn = _fooyin_conn()
    conn.execute("INSERT INTO Libraries VALUES (1, 'main', ?)", (str(tmp_path),))
    conn.execute("INSERT INTO Libraries VALUES (2, 'ext', '/definitely/not/mounted')")
    assert missing_library_roots(conn) == ["/definitely/not/mounted"]


def test_prune_refuses_while_a_library_root_is_missing(tmp_path):
    # An unmounted drive turns the entire library into orphans; pruning then
    # would wipe every play count in one command.
    path, conn = _write_db(tmp_path)
    conn.execute("INSERT INTO Libraries VALUES (1, 'ext', '/definitely/not/mounted')")
    _add_stats(conn, "GONE", 7)
    conn.commit()
    conn.close()
    from fb2fooyin.restore import prune_orphans

    with pytest.raises(RuntimeError, match="not on disk"):
        prune_orphans(str(path), ["GONE"])
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM TrackStats WHERE TrackHash='GONE'").fetchone() == (1,)
