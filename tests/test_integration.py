"""End-to-end checks against the bundled foobar metadb (and live fooyin.db if present).

The export smoke tests need only the committed ``data/metadb.sqlite``. The
coverage test additionally needs the user's live ``fooyin.db`` and is skipped
where it is absent.
"""

import sqlite3
from pathlib import Path

import pytest

from fb2fooyin import export as export_mod
from fb2fooyin.importer import plan_changes

_METADB = Path(__file__).resolve().parents[1] / "data" / "metadb.sqlite"
_FOOYIN = Path("~/.local/share/fooyin/fooyin.db").expanduser()

pytestmark = pytest.mark.skipif(
    not _METADB.exists(), reason="bundled data/metadb.sqlite not present"
)


def test_default_foobar_db_is_package_relative():
    # the CLI default must resolve to the bundled data/metadb.sqlite, not a
    # hardcoded home path — otherwise a relocated checkout silently breaks.
    from fb2fooyin.__main__ import _DEFAULT_FOOBAR_DB

    assert _DEFAULT_FOOBAR_DB == str(_METADB)


def test_export_smoke():
    payload = export_mod.export(str(_METADB))
    assert payload["version"] == 4
    assert "stats_index_guid" in payload
    assert "generated_at" in payload
    assert payload["count"] > 1000
    r = payload["records"][0]
    assert set(r) >= {"hash", "hash_primary", "tail", "play_count", "rating_star"}
    assert all(len(rec["hash"]) == 32 for rec in payload["records"][:100])


def test_export_reproduces_known_hash():
    # XG - UNDEFEATED, recomputed from the real metadb.info tags end-to-end.
    payload = export_mod.export(str(_METADB))
    hashes = {r["hash"] for r in payload["records"]}
    assert "a8dccc8a8b87fd5b71693cef2697e02b" in hashes


@pytest.mark.skipif(not _FOOYIN.exists(), reason="live fooyin.db not present")
def test_hash_dominates_and_tail_only_mops_up():
    payload = export_mod.export(str(_METADB))
    conn = sqlite3.connect(f"file:{_FOOYIN}?mode=ro", uri=True)
    try:
        _changes, _unmatched, match = plan_changes(conn, payload["records"])
    finally:
        conn.close()
    matched = match.by_hash + match.by_primary + match.by_tail
    assert matched > 5000
    # content hashes (full + primary-artist) carry the overwhelming majority.
    assert (match.by_hash + match.by_primary) / matched > 0.99
    # the primary-artist hash recovers the multi-artist collabs fooyin filed
    # under the lead artist...
    assert match.by_primary > 0
    # ...leaving only a tiny broken-metadata residual for the path tail.
    assert match.by_tail < matched * 0.01
