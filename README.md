# fb2fooyin

**English** · [简体中文](README.zh-CN.md)

Migrate foobar2000 playback statistics into [fooyin](https://www.fooyin.org/) on Linux.

foobar2000's `foo_playcount` keeps its stats in `metadb.sqlite`. fooyin keeps
its own in `~/.local/share/fooyin/fooyin.db`. This tool moves five fields
across:

| fooyin column | foobar source |
|---|---|
| `PlayCount`   | play count |
| `FirstPlayed` | first played |
| `LastPlayed`  | last played |
| `AddedDate`   | added |
| `Rating`      | star rating |

No third-party dependencies — Python 3.10+ standard library only.

## How it matches tracks

The two libraries have different roots, path separators and genre-folder case
(`D:\11_MusicLib\11.11_C-Pop\…` vs `/home/xre/11_music/11.11_c-pop/…`), but the
album-relative tail below the genre folder (`artist/album/file`) is identical.
That case-folded tail is the join key (99.9% hit rate). fooyin keys its stats
by a content-based `TrackHash`, so import resolves `tail → Tracks.FilePath →
TrackHash → TrackStats`.

## Usage

Run through [uv](https://docs.astral.sh/uv/) — it builds the (dependency-free)
package into an isolated environment on first run, no manual venv needed:

```bash
cd _tool/fb2fooyin
uv sync                      # optional: create the environment up front

# 1. Export foobar stats to JSON (reads data/metadb.sqlite by default, read-only)
uv run fb2fooyin export --out stats.json

# 2. Preview the import (dry run — writes nothing). Close fooyin first.
uv run fb2fooyin import --json stats.json

# 3. Apply for real (backs up fooyin.db first, refuses if fooyin is running)
uv run fb2fooyin import --json stats.json --apply
```

Defaults point at this library's paths, so bare `export` / `import` work too.
No uv? The package is pure stdlib, so `python3 -m fb2fooyin …` works from this
directory as a fallback.

## Cross-checking a track (`inspect`)

To manually confirm a specific song is consistent between the two players,
`inspect` prints foobar and fooyin side by side (read-only, no writes). The
query is a case-insensitive substring of the album-relative path:

```bash
uv run fb2fooyin inspect "hypnotize"
```

```
xg/20260123_the core - 核 [e]_[qobuz-24-48-flac]/06. hypnotize.flac
  field        foobar                  fooyin
  play_count   54                      6
  rating       unrated (0xFF)          4.0★ (0.8)
  first_played 2026-01-23 11:22        2026-07-27 18:12
  last_played  2026-04-15 17:42        2026-08-01 13:55
  added        2026-01-23 11:19        2026-07-27 16:00
  fooyin_hash  —                       f3cf30dd86e5d8f4240b10f034c3f5cd
```

Handy before an import (see what will change) and after (confirm it landed).

## Merge rules (import)

Existing fooyin rows are merged, not blindly overwritten:

- **PlayCount** — additive but idempotent. A sidecar table `_fb2fooyin_import`
  records how much each run contributed, so re-running (or a grown foobar
  count) lands correctly and any plays fooyin itself logged in between survive.
- **FirstPlayed / AddedDate** — earliest known value wins.
- **LastPlayed** — latest value wins.
- **Rating** — foobar wins when it has a rating; otherwise fooyin's is kept.
  Pass `import --keep-fooyin-rating` to never overwrite a rating already set in
  fooyin (foobar still fills in the empty ones).
- A zero timestamp means "never" and never overwrites a real one.

## Safety

- Import is **dry-run by default**; `--apply` is required to write.
- `--apply` copies `fooyin.db` to `fooyin.db.bak-<timestamp>` first.
- It refuses to write if fooyin holds the database lock (close fooyin first).
- All writes run in a single transaction.

## Encodings (verified against the live databases)

- Timestamps: Windows FILETIME (100 ns since 1601) → Unix **milliseconds**.
- Rating byte → stars: `0x3F`=1, `0x6A`=2, `0x95`=3, `0xBF`=4, `0xEA`=5,
  `0xFF`=unrated. fooyin stores `star / 5` as a REAL (`-1.0` = unrated).

## Tests

```bash
uv run --with pytest pytest      # fetches pytest into an ephemeral env
```

See [DESIGN.md](DESIGN.md) for the full database-schema and merge design.

## License

[MIT](LICENSE) © Bananadurian
