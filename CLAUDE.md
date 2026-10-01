# CLAUDE.md

## 1. Authoritative documentation

Read the relevant document before changing its area; keep these references as pointers rather than copies of the documents.

- `README.md` / `README.zh-CN.md` — quick start and usage
- `DESIGN.md` / `DESIGN.zh-CN.md` — the sole authority for database-schema details (foobar index/BLOB layout and stats GUID, fooyin `Tracks`/`TrackStats` tables), the matching chain, merge semantics, and the safety model

## 2. Matching and merge contracts

Matching walks exact hash → primary-artist hash → case-folded hash → album-relative path tail; the folded layer covers tags that changed only in capitalisation (a re-download from another store). Import is dry-run by default; `--apply` backs up `fooyin.db` to `fooyin.db.bak-<timestamp>` first and refuses to write if fooyin holds the database lock. Play-count merge stays idempotent via a `_fb2fooyin_import` sidecar table; `restore` uses `max` instead so a snapshot→restore round trip with nothing changed is a no-op. Run via `uv` (pure stdlib, zero deps; `python3 -m fb2fooyin …` works as a fallback). See `DESIGN.md` for the exact field/offset contracts.
