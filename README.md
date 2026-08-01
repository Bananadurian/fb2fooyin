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

fooyin identifies a recording by a **content hash of its tags** (`TrackHash`),
not its path — so it survives files being moved or renamed. The tool reproduces
that exact hash from foobar's cached tags and matches on it directly (~99% of
tracks, and it recovers tracks whose paths since changed). For the small
residual it can't reproduce (e.g. multi-artist m4a, where the two players read
the artist list differently), it falls back to the **album-relative path tail**
— the `artist/album/file` segment below the genre folder, identical across the
two libraries despite different roots, separators and case. Combined coverage of
tracks present in both libraries: 100%.

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
  play_count   54                      60
  rating       unrated (0xFF)          4.0★ (0.8)
  first_played 2026-01-23 11:22        2026-01-23 11:22
  last_played  2026-04-15 17:42        2026-08-01 13:55
  added        2026-01-23 11:19        2026-01-23 11:19
  hash         f3cf30dd86e5…           f3cf30dd86e5…
  match        matched BY HASH ✓ (path-independent)
```

The `hash` row shows the foobar-recomputed vs fooyin-stored `TrackHash`; the
`match` row states whether the track resolved by hash or fell back to the tail.

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
