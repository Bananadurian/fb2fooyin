# fb2fooyin

**English** · [简体中文](README.zh-CN.md)

Migrate foobar2000 playback statistics into [fooyin](https://www.fooyin.org/) on Linux.

foobar2000's [`foo_playcount`](https://wiki.hydrogenaudio.org/index.php?title=Foobar2000:Components/Playback_Statistics_v3.x_%28foo_playcount%29)
keeps its stats in `metadb.sqlite`, inside the foobar2000 **profile** folder:

- **Portable** install — `<install_dir>\profile`, e.g. `D:\foobar2000\profile`
- **Standard** install — `%APPDATA%\foobar2000\profile`, i.e.
  `C:\Users\<user>\AppData\Roaming\foobar2000\profile`

fooyin keeps its own in `~/.local/share/fooyin/fooyin.db`. This tool moves five
fields across:

| fooyin column | foobar source |
|---|---|
| `PlayCount`   | play count |
| `FirstPlayed` | first played |
| `LastPlayed`  | last played |
| `AddedDate`   | added |
| `Rating`      | star rating |

No third-party dependencies — Python 3.10+ standard library only.

**Tested with:** foobar2000 v2.25.x (x64) · `foo_playcount` v3.x · fooyin v0.12.1.

## How it matches tracks

fooyin identifies a recording by a **content hash of its tags** (`TrackHash`),
not its path — so it survives files being moved or renamed. The tool reproduces
that exact hash from foobar's cached tags and matches on it directly. Multi-artist
tracks that fooyin filed under the lead artist get a **second hash** tried too
(the same formula over the primary artist only), so collabs match by content, not
by path.

Because the hash is over the raw tag strings, a track re-tagged with different
capitalisation (`A Strange Kind Of Love` → `A Strange Kind of Love`, typical of
re-downloading an album from another store) hashes to something completely
different. A **case-folded hash** is tried next and recovers those. Together the
three hashes cover **99.98%** of tracks — and, being tag-derived, they ignore
your directory layout entirely.

For the tiny broken-metadata residual the hashes can't reproduce (empty-tag files
and the like), it falls back to the **album-relative path tail** — the
`artist/album/file` segment below the genre folder, identical across the two
libraries despite different roots, separators and case. On a differently-organised
library those few just get reported as unmatched; the hash path is unaffected.

## Usage

Run through [uv](https://docs.astral.sh/uv/) — it builds the (dependency-free)
package into an isolated environment on first run, no manual venv needed:

```bash
git clone https://github.com/Bananadurian/fb2fooyin.git
cd fb2fooyin
uv sync                      # optional: create the environment up front
uv run fb2fooyin --help      # list all commands & flags

# 1. Export foobar stats to JSON (read-only). Put your foobar2000 profile's
#    metadb.sqlite at ./data/metadb.sqlite, or pass --foobar-db PATH. Writes
#    ./stats.json.
uv run fb2fooyin export

# 2. Preview the import — dry run, writes nothing. Close fooyin first. Reads
#    ./stats.json and ~/.local/share/fooyin/fooyin.db by default.
uv run fb2fooyin import

# 3. Apply for real (backs up fooyin.db first; refuses if fooyin is running).
uv run fb2fooyin import --apply
```

Every path has a default — override with `--foobar-db` / `--out` / `--json` /
`--fooyin-db`. No uv? The package is pure stdlib, so `python3 -m fb2fooyin …`
works as a fallback.

## Keeping your stats through file edits (`snapshot` / `restore`)

Once foobar is retired it can no longer help: plays you log in fooyin, and
albums foobar never saw, exist nowhere else. And when you re-tag or replace a
file, fooyin mints a new `TrackHash` and the old stats row is stranded — its
tags are deleted with the old row, and **a hash cannot be turned back into
tags**, so nothing after the fact can repair it.

So leave evidence first:

```bash
# Take a snapshot BEFORE editing files. One read, no writes.
uv run fb2fooyin snapshot --out snapshot.json

# …re-tag / rename / replace files, let fooyin rescan…

# Put the stats back on whatever the recording is called now (dry-run first).
uv run fb2fooyin restore --json snapshot.json
uv run fb2fooyin restore --json snapshot.json --apply
```

`restore` skips anything whose identity never broke, then matches by folded hash
(re-cased tags), then by exact file path (re-tagged in place). If both fail you
can enable a last-resort track-number + title pairing by naming the target
directory — it stays scoped and capped because that kind of guess is only safe
within one album:

```bash
uv run fb2fooyin restore --json snapshot.json --to "/path/to/the/new/album" 
```

Add `--prune-moved` to delete each source row once its stats have been written
elsewhere (deterministic matches only; a guessed pairing keeps its source row).

### Knowing something broke (`orphans`)

A stats row whose track is gone is the only signal you get. Check it now and
then — a non-empty list means something changed identity and was never repaired:

```bash
uv run fb2fooyin orphans --from-json snapshot.json    # read-only
uv run fb2fooyin orphans --prune                      # dry-run of the cleanup
uv run fb2fooyin orphans --prune --apply              # delete them
```

`--from-json` re-attaches a readable path to each orphan. Pruning refuses to run
while any library root is missing from disk — an unmounted drive makes the whole
library look orphaned.

### Automating the snapshot

The scheme depends on the snapshot existing *before* the edit. If you'd rather
not rely on remembering, a weekly systemd user timer bounds the worst case to
one week — create `~/.config/systemd/user/fb2fooyin-snapshot.service`:

```ini
[Unit]
Description=Snapshot fooyin playback stats

[Service]
Type=oneshot
WorkingDirectory=%h/path/to/fb2fooyin
ExecStart=/usr/bin/uv run fb2fooyin snapshot --out %h/.local/share/fb2fooyin/snapshot-%%Y%%m%%d.json
```

and `~/.config/systemd/user/fb2fooyin-snapshot.timer`:

```ini
[Unit]
Description=Weekly fooyin stats snapshot

[Timer]
OnCalendar=weekly
Persistent=true

[Install]
WantedBy=timers.target
```

then `systemctl --user enable --now fb2fooyin-snapshot.timer`.

## Cross-checking a track (`inspect`)

To manually confirm a specific song is consistent between the two players,
`inspect` prints foobar and fooyin side by side (read-only, no writes). The
query is a case-insensitive substring of the album-relative path:

```bash
uv run fb2fooyin inspect "hypnotize"
```

```
xg/20260123_the core - 核 [e]_[qobuz-24-48-flac]/06. hypnotize.flac
  field        foobar                  fooyin                  merged
  play_count   54                      60                      60
  rating       unrated (0xFF)          4.0★ (0.8)              4.0★ (0.8)
  first_played 2026-01-23 11:22        2026-01-23 11:22        2026-01-23 11:22
  last_played  2026-04-15 17:42        2026-08-01 13:55        2026-08-01 13:55
  added        2026-01-23 11:19        2026-01-23 11:19        2026-01-23 11:19
  hash         f3cf30dd86e5…           f3cf30dd86e5…
  match        matched BY HASH ✓ (path-independent)
```

`merged` is what an import would write next — same merge rules, sidecar-aware.
Here it equals `fooyin` because the track is already imported (re-running is a
safe no-op); on a fresh track it shows foobar's plays added to fooyin's, earliest
first/added and latest last. The `hash` row shows the foobar-recomputed vs
fooyin-stored `TrackHash`, and `match` states whether it resolved by hash or fell
back to the tail.

Handy before an import (see what will change) and after (confirm it landed).

## Merge rules

**Import (foobar → fooyin)** — additive, because the two players accumulated
plays independently:

- **PlayCount** — additive but idempotent. A sidecar table `_fb2fooyin_import`
  records how much each run contributed, so re-running (or a grown foobar
  count) lands correctly and any plays fooyin itself logged in between survive.
- **FirstPlayed / AddedDate** — earliest known value wins.
- **LastPlayed** — latest value wins.
- **Rating** — foobar wins when it has a rating; otherwise fooyin's is kept.
  Pass `import --keep-fooyin-rating` to never overwrite a rating already set in
  fooyin (foobar still fills in the empty ones).
- A zero timestamp means "never" and never overwrites a real one.
- Duplicate foobar entries for one file (a renamed library root leaves the old
  path spelling behind) are collapsed before merging — otherwise every play
  count gets multiplied by the number of stale roots.

**Restore (fooyin → its own snapshot)** — `max`, not additive. Restoring a
snapshot onto an unchanged database must be a no-op, so play count takes the
larger of the two rather than summing. The sidecar row moves with the stats, so
a later foobar import doesn't add its contribution a second time.

## Safety

- Every writing command is **dry-run by default**; `--apply` is required.
- Matches found only by path tail (`import`) or by track number + title
  (`restore`) are flagged **low-confidence** in the dry-run, so you can eyeball
  the few before `--apply`.
- `--apply` copies `fooyin.db` to `fooyin.db.bak-<timestamp>` first.
- To redo or undo an import, restore a `fooyin.db.bak-<timestamp>` — don't delete
  the `_fb2fooyin_import` sidecar by hand, which desyncs the play-count ledger and
  double-counts next time (see [DESIGN.md](DESIGN.md) §3.4).
- It refuses to write if fooyin holds the database lock (close fooyin first).
- Deleting rows is never a side effect: `--prune-moved` / `--prune` are separate,
  off-by-default switches, and the whole-library sweep additionally refuses while
  a library root is missing from disk.
- Each JSON records which command produced it, so feeding a snapshot to `import`
  (or an export to `restore`) fails loudly instead of applying the wrong rule.
- All writes run in a single transaction.

## Encodings (verified against the live databases)

- Timestamps: Windows FILETIME (100 ns since 1601) → Unix **milliseconds**.
- Rating byte → stars: `0x3F`=1, `0x6A`=2, `0x95`=3, `0xBF`=4, `0xEA`=5,
  `0xFF`=unrated. fooyin stores `star / 5` as a normalised REAL (`-1.0` =
  unrated) — so fooyin's rating-scale setting (1-5 / 1-10 / 1-100) only changes
  the display, not what's stored.

## Tests

```bash
uv run --with pytest pytest      # fetches pytest into an ephemeral env
```

See [DESIGN.md](DESIGN.md) for the full database-schema and merge design.

## License

[MIT](LICENSE) © Bananadurian
