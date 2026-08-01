# DESIGN — fb2fooyin

**English** · [简体中文](DESIGN.zh-CN.md)

Migrate foobar2000 playback statistics into fooyin. This document is the
authoritative record of **what the two databases store, where, and how the tool
reads/writes them**. Implementation lives in `fb2fooyin/`; this explains the
*why* and the on-disk contracts it depends on.

---

## 1. Goal & first principles

The tool is, at heart, two pure transforms around one intermediate file:

```
foobar2000 metadb.sqlite  ──export──▶  stats.json  ──import──▶  fooyin.db
        (read-only)                   (portable)              (transactional)
```

- **export** = read the foobar stats out of an opaque BLOB and normalise them
  into a stable, human-readable key/value form.
- **import** = match those records to fooyin tracks and merge five fields into
  fooyin's stats table.

The intermediate JSON is deliberately keyed by a *content-stable* identifier
(the album-relative path tail), not by a database rowid or absolute path, so the
export survives library reorganisation and the two stages stay decoupled.

Five fields are migrated: **play count, first played, last played, added date,
rating**.

---

## 2. foobar2000 side — `metadb.sqlite`

Written by the `foo_playcount` component. It is **not** a relational schema you
can query directly; it is a key/value store of opaque per-track BLOBs.

### 2.1. Tables

| Table | Columns | Role |
|---|---|---|
| `config` | `key TEXT UNIQUE`, `value TEXT` | component version flags (`version`, `oldRatingsFixed`, `uniNormFix`) |
| `metadb` | `name TEXT PK`, `info BLOB`, `infoBrowse BLOB`, `size`, `lastModified`, `infoBrowseTime`, `lastseen`, `created`, `attribs`, `attribsValid` | tag/technical cache per file (**not used** by this tool) |
| `metadb_indexes` | `name TEXT PK`, `synced INTEGER`, `retention INTEGER` | registry of the component indexes below |
| `metadb_index_<GUID>` | `key INTEGER`, `filename TEXT UNIQUE PK` | maps an integer key ⇄ the track's location string |
| `metadb_index_<GUID>_data` | `key INTEGER PK UNIQUE`, `value BLOB` | the per-track payload, joined to the name table on `key` |

Every foobar index is this **pair of tables** — a `_<GUID>` name table and a
`_<GUID>_data` payload table — joined on `key`.

### 2.2. The index we read

`metadb_indexes` in this library registers seven GUIDs. Only one carries the
playback statistics we want:

| GUID | Tool tables | Identified as | Used |
|---|---|---|---|
| **`C653739F-14B3-4EF2-819B-A3E2883230AE`** | `metadb_index_C653739F_14B3_4EF2_819B_A3E2883230AE` (+ `_data`) | **Playback Statistics** (count/first/last/added/rating) | **✔ yes** |
| `0C1BD000-…-48EE4249DEC3` | same pattern | per-play timestamp history (variable-length FILETIME array) | ✘ |
| `0C1BD000-…-48EE4249DED0` | same pattern | 20-byte record, unidentified | ✘ |
| `EF148A2E-…`, `0FEBCD4A-…`, `E58A4298-…`, `7D07305A-…` | same pattern | unidentified / empty `_data` | ✘ |

> In SQLite the GUID dashes become underscores in the actual table names. The
> tool hardcodes the underscore form as `core.STATS_INDEX_GUID`.

### 2.3. `filename` (the name table key)

Format: `"<subsong>+<location>"`, e.g.

```
0+file://D:\11_MusicLib\11.11_C-Pop\Fine乐团\2015 I’m Sorry [16-44-WAV]\01. …wav
```

- `0+` is the subsong index prefix. This library has no CUE sheets, so it is
  always `0+`.
- The location is a foobar URI with **Windows** backslashes and drive letter,
  captured before the Linux migration.
- Non-file rows (radio streams `http://…`, `unpack://zip|…`) also live here and
  are skipped because they carry no genre folder (see §4).

### 2.4. `value` BLOB layout (Playback Statistics)

Fixed **40 bytes**, little-endian. Verified against live data
(`core.parse_stats_blob`):

| Offset | Size | Type | Field |
|---|---|---|---|
| `0` | 4 | `uint32` | **play count** |
| `4` | 4 | — | zero padding |
| `8` | 8 | `FILETIME` | **first played** |
| `16` | 8 | `FILETIME` | **last played** |
| `24` | 8 | `FILETIME` | **added** |
| `32` | 1 | `uint8` | **rating** (`0xFF` = unrated) |
| `33` | 7 | — | zero padding |

- **FILETIME** = 100-nanosecond ticks since 1601-01-01. `0` means "never".
- **Rating byte** uses a roughly linear (~42.5 apart) encoding. Anchors read
  from the real db:

  | byte | `0x3F` | `0x6A` | `0x95` | `0xBF` | `0xEA` | `0xFF` |
  |---|---|---|---|---|---|---|
  | stars | 1 | 2* | 3 | 4 | 5 | unrated |

  `*` 2★ (`0x6A`) is interpolated — no 2★ track exists in this library. Decoding
  uses nearest-anchor matching so a byte one tick off still resolves.

---

## 3. fooyin side — `fooyin.db`

Location: `~/.local/share/fooyin/fooyin.db`. A normal relational schema.

### 3.1. Tables (relevant subset)

| Table | Role |
|---|---|
| `Libraries` | library roots — here `id=2, name=11_music, path=/home/xre/11_music` |
| `Tracks` | one row per audio file (tags + technical + hash) |
| `TrackStats` | playback statistics, **keyed by `TrackHash`** — the write target |
| `Playlists`, `PlaylistTracks`, `PlaybackQueue`, `Settings`, `TracksView` | unrelated |

### 3.2. `Tracks` (read-only — used to resolve the hash)

| Column | Type | Notes |
|---|---|---|
| `TrackID` | INTEGER PK AUTOINCREMENT | |
| `FilePath` | TEXT NOT NULL | absolute Linux path, lower-cased genre folders, e.g. `/home/xre/11_music/11.11_c-pop/…` |
| `Subsong` | INTEGER DEFAULT 0 | always 0 in this library |
| `TrackHash` | TEXT | **content-based** hash (artist/album/title/…), **not** derived from the path |
| *(many tag/tech columns)* | | `Title`, `Artists`, `Album`, `Duration`, `Codec`, … — not used |

Key property (verified): the **same `TrackHash` appears at multiple different
`FilePath`s** (duplicate album copies), confirming the hash is metadata-derived.
`UNIQUE(FilePath, Offset, Subsong)`.

### 3.3. `TrackStats` (the write target)

```sql
CREATE TABLE TrackStats (
    TrackHash   TEXT PRIMARY KEY,
    LastSeen    INTEGER,
    AddedDate   INTEGER,
    FirstPlayed INTEGER,
    LastPlayed  INTEGER,
    PlayCount   INTEGER DEFAULT 0,
    Rating      REAL DEFAULT 0
);
```

| Column | Unit / convention | Written by tool |
|---|---|---|
| `TrackHash` | primary key, from `Tracks` | join key |
| `LastSeen` | Unix ms | left untouched (fooyin-owned) |
| `AddedDate` | Unix ms | ✔ merged (earlier wins) |
| `FirstPlayed` | Unix ms (`0` = never) | ✔ merged (earlier wins) |
| `LastPlayed` | Unix ms (`0` = never) | ✔ merged (later wins) |
| `PlayCount` | integer | ✔ merged (idempotent additive) |
| `Rating` | REAL `star/5`; `-1.0` = unrated | ✔ merged (foobar wins if set) |

Observed values confirming the conventions: `Rating` of `0.4` (=2★), `0.8`
(=4★), `-1.0` (unrated); timestamps are 13-digit Unix **milliseconds**.

### 3.4. Sidecar table created by the tool

```sql
CREATE TABLE _fb2fooyin_import (
    TrackHash            TEXT PRIMARY KEY,
    ContributedPlayCount INTEGER NOT NULL,
    ImportedAt           INTEGER NOT NULL   -- Unix ms
);
```

Records how many plays *this tool* last contributed to each hash, so the
additive `PlayCount` merge is idempotent across re-runs (see §5). fooyin ignores
tables it does not know about; it travels with the db through backup/restore.

---

## 4. The matching key — album-relative path tail

The central design decision. The two libraries disagree on everything *above*
the album:

| | foobar | fooyin |
|---|---|---|
| root | `D:\11_MusicLib\` | `/home/xre/11_music/` |
| separator | `\` | `/` |
| genre folder case | `11.11_C-Pop` | `11.11_c-pop` |

…but the segment **below the genre folder — `artist/album/file` — is
byte-identical** (album dirs and filenames were never renamed, only the upper
folders were lower-cased during the Windows→Linux move).

So the key is the **case-folded tail after the `11.NN_…` genre segment**:

```
0+file://D:\11_MusicLib\11.11_C-Pop\Fine乐团\Album\01. Song.wav
/home/xre/11_music/11.11_c-pop/Fine乐团\Album/01. Song.wav
                    └────────────────┬───────────────────────┘
        both →  fine乐团/album/01. song.wav
```

Implemented in `core.path_tail`: strip the `N+` subsong prefix, drop `file://`,
normalise separators, lower-case, then take everything after the
`/11.\d\d…/` genre folder. Paths with no genre folder (radio, zip-embedded)
return `None` and are skipped.

**Measured hit rate: 9729 / 9740 fooyin tracks (99.9%).** The 11 misses are all
zip-embedded (`unpack://zip|…`) tracks with no on-disk genre path.

Because fooyin keys stats by `TrackHash`, import chains:

```
tail ──(fooyin Tracks)──▶ TrackHash ──▶ TrackStats
```

The tool never needs to reproduce fooyin's hash algorithm.

---

## 5. Merge semantics (import)

Existing fooyin rows are merged field-by-field, never blindly overwritten:

| Field | Rule | Idempotent? |
|---|---|---|
| `FirstPlayed` | `min` of non-zero values | ✔ |
| `AddedDate` | `min` of non-zero values | ✔ |
| `LastPlayed` | `max` | ✔ |
| `Rating` | foobar value if rated, else keep fooyin | ✔ |

`--keep-fooyin-rating` flips the rating precedence: a rating already set in
fooyin is never overwritten, though foobar still fills in the ones fooyin
lacks.
| `PlayCount` | `current − last_contribution + foobar` | ✔ via sidecar |

The `PlayCount` rule is the only non-trivial one. Naive addition double-counts on
re-run; naive overwrite discards plays fooyin logged itself. The sidecar stores
each run's contribution, so:

```
new = current_playcount − previously_contributed + foobar_playcount
```

- first run: `contributed = 0` → adds the full foobar count;
- re-run, unchanged foobar: adds `0` (idempotent);
- foobar grew by N: adds exactly N;
- user played it in fooyin between runs: that increment is preserved.

A zero foobar timestamp is treated as "unknown" and never overwrites a real
fooyin value.

**Duplicate copies.** Several library copies of one recording share a single
fooyin `TrackHash` (§3.2), so more than one export record can target the same
stats row. They are aggregated by hash *before* the merge above — play counts
summed (an unplayed backup copy adds 0), earliest first/added, latest last,
highest rating — and the row is written once. Without this the records would
race to write one row, giving a non-deterministic, non-idempotent result.

---

## 6. Safety model

- Import is **dry-run by default**; `--apply` is required to write. Dry-run
  opens fooyin **read-only** and prints the change plan (insert/update counts,
  unmatched list, sample diffs).
- `--apply` copies `fooyin.db` → `fooyin.db.bak-<timestamp>` before any write.
- `--apply` issues `BEGIN IMMEDIATE`; if fooyin holds the lock it aborts with a
  "close fooyin first" error rather than risk a corrupt/partial write.
- All writes commit in a single transaction.

---

## 7. Boundaries & non-goals

- **No tag reading.** Matching is purely path-tail based; the tool never opens
  audio files or parses foobar's `metadb.info` tag BLOBs.
- **No hash reproduction.** fooyin's `TrackHash` is looked up, not recomputed.
- **CUE / subsongs** are out of scope (this library has none; `Subsong` is
  always 0). Supporting them would mean adding subsong to the matching key.
- **One direction only** (foobar → fooyin). There is no fooyin → foobar path.
- The Playback Statistics GUID is treated as a fixed constant for this
  library's `metadb.sqlite`; a different foobar profile could use a different
  GUID and would need `core.STATS_INDEX_GUID` updated.
