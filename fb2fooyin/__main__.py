"""Command-line entry point for fb2fooyin."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import export as export_mod
from . import importer as import_mod
from . import inspect as inspect_mod
from . import restore as restore_mod
from . import snapshot as snapshot_mod

_DEFAULT_FOOBAR_DB = str(Path(__file__).resolve().parents[1] / "data" / "metadb.sqlite")
_DEFAULT_FOOYIN_DB = os.path.expanduser("~/.local/share/fooyin/fooyin.db")


def _cmd_export(args: argparse.Namespace) -> int:
    payload = export_mod.export(args.foobar_db)
    export_mod.write_json(payload, args.out)
    print(
        f"exported {payload['count']} records -> {args.out}\n"
        f"  no genre tail (hash-only):  {payload['no_tail']}\n"
        f"  skipped (bad stats blob):   {payload['skipped_bad_blob']}"
    )
    return 0


def _cmd_snapshot(args: argparse.Namespace) -> int:
    payload = snapshot_mod.snapshot(args.fooyin_db, args.path)
    export_mod.write_json(payload, args.out)
    print(
        f"snapshot {payload['count']} records -> {args.out}\n"
        f"  tracks with no stats (skipped): {payload['no_stats']}"
    )
    if args.path:
        print(f"  scoped to: {args.path}")
    return 0


def _fmt(change: import_mod.Change) -> str:
    kind = "INSERT" if change.is_insert else "update"
    return f"  [{kind}] {change.tail}\n      old={change.old}\n      new={change.new}"


def _cmd_import(args: argparse.Namespace) -> int:
    payload = import_mod.load_payload(args.json, expect_kind=export_mod.KIND)
    records = payload["records"]

    import sqlite3

    uri = f"file:{args.fooyin_db}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        changes, unmatched, match = import_mod.plan_changes(
            conn, records, keep_fooyin_rating=args.keep_fooyin_rating
        )
    finally:
        conn.close()

    inserts = sum(1 for c in changes if c.is_insert)
    updates = len(changes) - inserts
    matched = match.by_hash + match.by_primary + match.by_norm + match.by_tail
    print(
        f"records: {len(records)}  "
        f"matched: {matched} "
        f"(by hash {match.by_hash}, by primary {match.by_primary}, "
        f"by norm {match.by_norm}, by tail {match.by_tail})  "
        f"unmatched: {match.unmatched}\n"
        f"changes: {len(changes)} (insert {inserts}, update {updates})"
    )
    for c in changes[: args.sample]:
        print(_fmt(c))
    if unmatched:
        print(f"  unmatched sample: {unmatched[: args.sample]}")

    if match.dedup_conflicts:
        print(
            f"\nnote: {len(match.dedup_conflicts)} track(s) had duplicate foobar "
            f"entries disagreeing on play count — took the maximum:"
        )
        for tail in match.dedup_conflicts[: args.sample]:
            print(f"  {tail}")

    low_conf = [c for c in changes if c.source == "tail"]
    if low_conf:
        print(
            f"\n⚠ low-confidence: {len(low_conf)} matched by path tail, not "
            f"content hash — review before --apply:"
        )
        for c in low_conf[: args.sample]:
            print(_fmt(c))

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to write.")
        return 0

    backup = import_mod.apply_changes(args.fooyin_db, changes)
    print(f"\napplied {len(changes)} changes. backup: {backup}")
    return 0


def _fmt_restore(ch: restore_mod.Change) -> str:
    kind = "INSERT" if ch.is_insert else "update"
    mark = "" if ch.source in restore_mod.DETERMINISTIC else "  ⚠"
    return (
        f"  [{kind}] ({ch.source}){mark} {ch.file_path}\n"
        f"      {ch.source_hash[:12]}… -> {ch.target_hash[:12]}…\n"
        f"      old={ch.old}\n      new={ch.new}"
    )


def _cmd_restore(args: argparse.Namespace) -> int:
    payload = import_mod.load_payload(args.json, expect_kind=snapshot_mod.KIND)
    records = payload["records"]

    import sqlite3

    uri = f"file:{args.fooyin_db}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        changes, unresolved, report = restore_mod.plan_restore(
            conn, records, to_prefix=args.to, fuzzy_limit=args.fuzzy_limit
        )
    finally:
        conn.close()

    print(
        f"records: {len(records)}  intact: {report.intact}  "
        f"moved: {report.by_norm + report.by_path + report.by_fuzzy} "
        f"(by norm {report.by_norm}, by path {report.by_path}, by fuzzy {report.by_fuzzy})  "
        f"unresolved: {report.unresolved}\n"
        f"changes: {len(changes)}"
    )
    if not report.fuzzy_available:
        reason = "no --to given" if not args.to else f"more than {args.fuzzy_limit} records"
        print(f"  (track-number+title fallback disabled: {reason})")
    for ch in changes[: args.sample]:
        print(_fmt_restore(ch))
    if unresolved:
        print(f"  unresolved sample: {unresolved[: args.sample]}")

    fuzzy = [c for c in changes if c.source == "fuzzy"]
    if fuzzy:
        print(
            f"\n⚠ {len(fuzzy)} matched by track number + title, not by content "
            f"hash or path — check each pairing before --apply. "
            f"These are never pruned, even with --prune-moved."
        )

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to write.")
        return 0

    backup = restore_mod.apply_restore(args.fooyin_db, changes, prune_moved=args.prune_moved)
    pruned = (
        sum(1 for c in changes if c.source in restore_mod.DETERMINISTIC)
        if args.prune_moved
        else 0
    )
    print(f"\nrestored {len(changes)} rows (pruned {pruned} source rows). backup: {backup}")
    return 0


def _cmd_orphans(args: argparse.Namespace) -> int:
    import sqlite3

    known: dict[str, str] = {}
    if args.from_json:
        payload = import_mod.load_payload(args.from_json)
        for r in payload.get("records", []):
            if r.get("hash"):
                known.setdefault(r["hash"], r.get("file_path") or r.get("tail") or "")

    uri = f"file:{args.fooyin_db}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = restore_mod.list_orphans(conn, known=known, include_empty=args.all)
        missing = restore_mod.missing_library_roots(conn)
    finally:
        conn.close()

    print(f"{len(rows)} orphaned stats row(s){'' if args.all else ' with plays'}:")
    for o in rows:
        residual = o.play_count - (o.contributed or 0)
        print(
            f"  {o.track_hash[:12]}…  plays={o.play_count}"
            f"  (fooyin-only {residual})  {o.known_path or '<unknown path>'}"
        )
    if missing:
        print(f"\n⚠ library root(s) not on disk: {', '.join(missing)}")

    if not args.prune:
        return 0
    if not rows:
        print("\nnothing to prune.")
        return 0
    print(
        "\n⚠ pruning deletes these rows permanently. A break you have not "
        "repaired yet looks exactly like this — restore it first, or its plays "
        "are gone."
    )
    if not args.apply:
        print("DRY RUN — nothing written. Re-run with --prune --apply to delete.")
        return 0
    backup = restore_mod.prune_orphans(args.fooyin_db, [o.track_hash for o in rows])
    print(f"\npruned {len(rows)} rows. backup: {backup}")
    return 0


def _cmd_inspect(args: argparse.Namespace) -> int:
    print(inspect_mod.render(args.foobar_db, args.fooyin_db, args.query, args.limit))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fb2fooyin",
        description="Migrate foobar2000 play stats (count / first / last / added / rating) into fooyin.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pe = sub.add_parser("export", help="foobar metadb -> JSON")
    pe.add_argument("--foobar-db", default=_DEFAULT_FOOBAR_DB)
    pe.add_argument("--out", default="stats.json")
    pe.set_defaults(func=_cmd_export)

    ps = sub.add_parser("snapshot", help="fooyin.db -> JSON (take one BEFORE editing files)")
    ps.add_argument("--fooyin-db", default=_DEFAULT_FOOYIN_DB)
    ps.add_argument("--out", default="snapshot.json")
    ps.add_argument(
        "--path",
        default=None,
        help="only snapshot tracks under this absolute path prefix (default: whole library)",
    )
    ps.set_defaults(func=_cmd_snapshot)

    pi = sub.add_parser("import", help="JSON + fooyin.db -> TrackStats")
    pi.add_argument("--json", default="stats.json")
    pi.add_argument("--fooyin-db", default=_DEFAULT_FOOYIN_DB)
    pi.add_argument("--apply", action="store_true", help="actually write (default is dry-run)")
    pi.add_argument(
        "--keep-fooyin-rating",
        action="store_true",
        help="never overwrite a rating already set in fooyin (foobar still fills empty ones)",
    )
    pi.add_argument("--sample", type=int, default=10, help="sample rows to print")
    pi.set_defaults(func=_cmd_import)

    pr = sub.add_parser("restore", help="fooyin snapshot JSON -> TrackStats (repair broken identities)")
    pr.add_argument("--json", default="snapshot.json")
    pr.add_argument("--fooyin-db", default=_DEFAULT_FOOYIN_DB)
    pr.add_argument("--apply", action="store_true", help="actually write (default is dry-run)")
    pr.add_argument(
        "--to",
        default=None,
        help="target directory; enables the track-number+title fallback within it",
    )
    pr.add_argument(
        "--fuzzy-limit",
        type=int,
        default=100,
        help="max records for the track-number+title fallback to stay enabled",
    )
    pr.add_argument(
        "--prune-moved",
        action="store_true",
        help="delete each source row once its stats were written elsewhere "
        "(deterministic matches only)",
    )
    pr.add_argument("--sample", type=int, default=10, help="sample rows to print")
    pr.set_defaults(func=_cmd_restore)

    po = sub.add_parser("orphans", help="list stats rows whose track is gone (read-only by default)")
    po.add_argument("--fooyin-db", default=_DEFAULT_FOOYIN_DB)
    po.add_argument(
        "--from-json",
        default=None,
        help="snapshot or export JSON used to recover each orphan's original path",
    )
    po.add_argument("--all", action="store_true", help="include rows with no plays")
    po.add_argument("--prune", action="store_true", help="delete the listed rows")
    po.add_argument("--apply", action="store_true", help="with --prune, actually delete")
    po.set_defaults(func=_cmd_orphans)

    pn = sub.add_parser("inspect", help="show foobar vs fooyin stats for a track (read-only)")
    pn.add_argument("query", help="case-insensitive substring of the album-relative path")
    pn.add_argument("--foobar-db", default=_DEFAULT_FOOBAR_DB)
    pn.add_argument("--fooyin-db", default=_DEFAULT_FOOYIN_DB)
    pn.add_argument("--limit", type=int, default=20, help="max matches to print")
    pn.set_defaults(func=_cmd_inspect)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (RuntimeError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
