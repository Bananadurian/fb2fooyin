"""Command-line entry point for fb2fooyin."""

from __future__ import annotations

import argparse
import os
import sys

from . import export as export_mod
from . import importer as import_mod
from . import inspect as inspect_mod

_DEFAULT_FOOBAR_DB = os.path.expanduser("~/11_music/_tool/fb2fooyin/data/metadb.sqlite")
_DEFAULT_FOOYIN_DB = os.path.expanduser("~/.local/share/fooyin/fooyin.db")


def _cmd_export(args: argparse.Namespace) -> int:
    payload = export_mod.export(args.foobar_db)
    export_mod.write_json(payload, args.out)
    print(
        f"exported {payload['count']} records -> {args.out}\n"
        f"  skipped (no genre tail): {payload['skipped_no_tail']}\n"
        f"  skipped (bad blob):      {payload['skipped_bad_blob']}"
    )
    return 0


def _fmt(change: import_mod.Change) -> str:
    kind = "INSERT" if change.is_insert else "update"
    return f"  [{kind}] {change.tail}\n      old={change.old}\n      new={change.new}"


def _cmd_import(args: argparse.Namespace) -> int:
    payload = import_mod.load_payload(args.json)
    records = payload["records"]

    import sqlite3

    uri = f"file:{args.fooyin_db}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        changes, unmatched = import_mod.plan_changes(
            conn, records, keep_fooyin_rating=args.keep_fooyin_rating
        )
    finally:
        conn.close()

    inserts = sum(1 for c in changes if c.is_insert)
    updates = len(changes) - inserts
    print(
        f"records: {len(records)}  matched-changes: {len(changes)} "
        f"(insert {inserts}, update {updates})  unmatched: {len(unmatched)}"
    )
    for c in changes[: args.sample]:
        print(_fmt(c))
    if unmatched:
        print(f"  unmatched sample: {unmatched[: args.sample]}")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to write.")
        return 0

    backup = import_mod.apply_changes(args.fooyin_db, changes)
    print(f"\napplied {len(changes)} changes. backup: {backup}")
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
