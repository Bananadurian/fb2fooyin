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

- **export** = read the foobar stats out of an opaque BLOB, read each track's
  tags out of foobar's tag cache, and normalise them into a stable,
  human-readable key/value form — including a **reproduced fooyin `TrackHash`**.
- **import** = match those records to fooyin tracks — by content hash first,
  path tail as fallback — and merge five fields into fooyin's stats table.

The intermediate JSON carries two *content-stable* identifiers per record: the
reproduced fooyin **`TrackHash`** (primary — a hash of the tags, immune to any
path change) and the **album-relative path tail** (fallback — for the few tracks
whose hash can't be reproduced). Neither is a database rowid or absolute path,
so the export survives library reorganisation and the two stages stay decoupled.

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
| `metadb` | `name TEXT PK`, `info BLOB`, `infoBrowse BLOB`, `size`, `lastModified`, `infoBrowseTime`, `lastseen`, `created`, `attribs`, `attribsValid` | tag/technical cache per file; **`info` is read for tags** to reproduce the fooyin hash (§2.5, §4) |
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

### 2.5. `metadb.info` BLOB layout (tags)

The `metadb` table's `info` BLOB is foobar's per-file tag cache. The tool reads
it to reproduce the fooyin hash (§4). Joined to the stats index on
`metadb.name = metadb_index_<GUID>.filename` (**26201/26201 rows join exactly**).

After a binary header (replaygain floats, MusicBrainz ids, …) the tags are
NUL-delimited tokens laid out as **`KEY \0 VALUE [\0 VALUE …] \0`** groups (an
empty token terminates each group; a key with several values is multi-valued,
e.g. two `ARTIST`s). Two quirks, both verified across formats and handled by
`core.parse_info_tags`:

- **Key case follows the source format.** FLAC/Vorbis keys are upper-case
  (`TITLE`, `TRACKNUMBER`); MP4/m4a keys are lower-case (`title`, `tracknumber`).
  Keys are therefore matched **case-insensitively**.
- **The first tag (`ALBUM`) is glued to the header** with no NUL before it, so it
  never appears as a clean token. It is recovered by its unique `ALBUM` suffix
  (`ALBUM ARTIST` / `ALBUMARTISTSORT` do not end in it).

Tag-less rips (some WAV) carry no readable tag tokens → empty fields → the record
hash-misses and falls back to the tail (§4).

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
| `TrackHash` | TEXT | **content-based** hash (artist/album/title/…), **not** derived from the path. The tool **reproduces** this hash from foobar's tags and matches on it (§4) |
| *(many tag/tech columns)* | | `Title`, `Artists`, `Album`, … — **not read** on the fooyin side; the hash is reproduced from *foobar's* tags and compared to `TrackHash` |

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

## 4. The matching key — reproduced fooyin `TrackHash`, path tail as fallback

fooyin identifies a recording by a **content hash of its tags**, not its path, so
the hash is immune to files being moved or renamed. Reproducing that hash from
foobar's cached tags lets the tool match on identity; the album-relative path
tail is kept only as a fallback for the few tracks whose hash can't be
reproduced.

### 4.1. Primary — the reproduced hash

Verified against fooyin's source (`src/core/track.cpp` `Track::generateHash` +
`include/utils/crypto.h` `Utils::generateHash`), the hash is the lower-case hex
**MD5** of the UTF-8 concatenation (**no separator**) of, in order:

```
artists.join(",")  ++  album  ++  discNumber  ++  trackNumber  ++  title  ++  str(subsong)
```

— the **raw tag strings**, no case folding. `core.fooyin_track_hash` reproduces
it; `core.parse_info_tags` supplies the fields from foobar's `metadb.info`
(§2.5); subsong comes from the `N+` filename prefix (§2.3).

Reproduced **100% (9740/9740)** against the live `fooyin.db` using fooyin's own
stored fields — i.e. the algorithm is exact.

> Edge case: fooyin falls back to `directory + filename` when the title is
> empty. That can't be reproduced from foobar's Windows paths, so a title-less
> track hash-misses and falls back to the tail.

### 4.2. Fallback — album-relative path tail

Kept for the residual the hash can't reproduce (§4.3). The two libraries
disagree on everything *above* the album:

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
return `None` — such records can still match by hash.

### 4.3. Resolution order & measured coverage

Per record, import resolves to a fooyin `TrackHash` (`importer._resolve`):

```
hash in fooyin?  ──yes──▶  that TrackHash            (primary, path-independent)
       └─no─▶  tail in fooyin Tracks?  ──yes──▶  TrackHash   (fallback)
                     └─no─▶  unmatched (track absent from fooyin)
```

then `TrackHash ──▶ TrackStats`. Measured on this library (foobar **25 175**
records vs fooyin **9 740** tracks):

| Resolution | Count | Note |
|---|---|---|
| by hash | 24 909 | path-independent; **recovers ~1 134** tracks the tail alone misses (moved / renamed / reorganised) |
| by tail (fallback) | 238 | all multi-artist m4a — foobar keeps the featured artists, fooyin stores only the primary, so the reproduced hashes differ |
| unmatched | 28 | not in fooyin at all (deleted albums, radio) → correctly skipped |

Combined coverage of the tracks present in both libraries: **100%, zero tail
ambiguity.** Hash and tail are complementary — the hash survives the renames the
tail can't, the tail covers the tag-parse residual the hash can't.

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

- **No audio-file reading.** Tags come from foobar's `metadb.info` cache
  (§2.5), never by opening the audio files — so export depends only on the
  metadb and still works for files offloaded to cloud storage.
- **Hash reproduction, not audio hashing.** fooyin's `TrackHash` is recomputed
  from tags (§4.1); the tool never hashes audio content. Path-tail matching
  remains as the fallback (§4.2).
- **CUE / subsongs.** `subsong` is part of the reproduced hash (`str(subsong)`),
  so multi-subsong tracks would hash correctly — but this library has none
  (`Subsong` is always 0), so it is untested.
- **One direction only** (foobar → fooyin). There is no fooyin → foobar path.
- The Playback Statistics GUID is treated as a fixed constant for this
  library's `metadb.sqlite`; a different foobar profile could use a different
  GUID and would need `core.STATS_INDEX_GUID` updated.
