# DESIGN — fb2fooyin

**English** · [简体中文](DESIGN.zh-CN.md)

Migrate foobar2000 playback statistics into fooyin. This document is the
authoritative record of **what the two databases store, where, and how the tool
reads/writes them**. Implementation lives in `fb2fooyin/`; this explains the
*why* and the on-disk contracts it depends on.

---

## 1. Goal & first principles

The tool is a set of pure transforms around **one intermediate file format**:

```
foobar2000 metadb.sqlite  ──export────▶  stats.json    ──import───▶  fooyin.db
        (read-only)                                                 (transactional)

fooyin.db  ──snapshot──▶  snapshot.json  ──restore──▶  fooyin.db
(read-only)                                           (transactional)
```

- **export** = read the foobar stats out of an opaque BLOB, read each track's
  tags out of foobar's tag cache, and normalise them into a stable,
  human-readable key/value form — including a **reproduced fooyin `TrackHash`**.
- **import** = match those records to fooyin tracks — by content hash first,
  path tail as fallback — and merge five fields into fooyin's stats table.
- **snapshot** = the same record format, produced from fooyin itself.
- **restore** = put a snapshot's stats back onto whatever the recording is
  called *now*, repairing identities that broke after the snapshot was taken.

Because the JSON is a format rather than a channel, the producer is
interchangeable: the same consumer logic serves a foreign library (foobar) and
the library's own past (a snapshot). Each payload is tagged with its `kind` so
the two are never fed to the wrong consumer — they share the record schema but
**not** the merge semantics (§5).

Each record carries *content-stable* identifiers, never a rowid or absolute
path, so it survives library reorganisation and the stages stay decoupled:

| Identifier | Survives | Fails when |
|---|---|---|
| `hash` — reproduced fooyin `TrackHash` | any move, rename, reorganisation | a tag changes |
| `hash_primary` — same, lead artist only | fooyin filing a collab under one artist | — |
| `hash_norm` — same, case-folded | tags re-cased by a re-download or re-tag | wording changes |
| `tail` / `file_path` — path | tags rewritten in place | the folder is renamed |

The last two rows are the lesson this design was rewritten around: hash and path
fail under *opposite* conditions, so keeping both is what makes recovery
possible. Losing an album's history takes breaking them at the same time — which
is exactly what re-downloading an album into a renamed folder does.

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

> In SQLite the GUID dashes become underscores in the actual table names.
> `core.detect_stats_guid` finds it automatically — the one non-empty index whose
> `_data` blobs are *uniformly* 40 bytes (the history index above also holds some
> 40-byte rows, so the discriminator is "all 40", not "has a 40"). The
> underscore-form constant `core.STATS_INDEX_GUID` is the fallback when detection
> is inconclusive, and `export` writes the resolved GUID into the JSON
> (`stats_index_guid`).

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
| `Title`, `Artists`, `Album`, `DiscNumber`, `TrackNumber` | TEXT | read **only** to recompute hashes — see the boundary below |

`Artists` is a list joined with the unit separator `\x1f`.

Key property (verified): the **same `TrackHash` appears at multiple different
`FilePath`s** (duplicate album copies), confirming the hash is metadata-derived.
`UNIQUE(FilePath, Offset, Subsong)`.

> **Boundary — reading fooyin's tag columns.** These columns are read for
> exactly one purpose: recomputing a hash (`core.read_fooyin_tracks`,
> `core.build_norm_index`). Tag *values* are never compared between the two
> libraries. The distinction matters: hash equality is a deterministic identity
> test, whereas comparing fields is fuzzy matching, which this tool does not do
> (§8). Verified: recomputing `fooyin_track_hash` from these columns reproduces
> **9802/9802** stored hashes, so the folded variant built from them is exact
> rather than approximate.

No soft delete. When a file's tags change, fooyin drops the old `Tracks` row and
inserts a new one under a new hash — the old identity, tags included, is gone
from the database. Its `TrackStats` row survives as an **orphan** keyed by a
hash nothing can be derived from any more (§6).

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

> fooyin's rating **scale** setting (`0-1` / `1-5` / `1-10` / `1-100`) only
> affects display and file-tag read/write
> (`src/core/engine/input/ratingtagpolicy.cpp`); `TrackStats.Rating` is always
> this normalised `0.0–1.0` float (`Track::rating()`), so the tool's `star/5`
> write is scale-independent.

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

Created on the **first `--apply`** (never by `export`, `inspect`, or a dry run).
Treat it as a **receipt, not a cache**: it is the half of the play-count ledger
that lives outside `TrackStats`. Hand-deleting it undoes nothing — it only drops
the "back out the last contribution" step, so the next import double-counts. With
fooyin at `5` and foobar at `10`:

| run | `PlayCount` merge | result |
|---|---|---|
| first `--apply` | `5 − 0 + 10` | `15` |
| re-run, sidecar kept | `15 − 10 + 10` | `15` (idempotent) |
| re-run, sidecar deleted | `15 − 0 + 10` | `25` (double-counted) |

To redo or undo an import cleanly, restore the backup (§6) instead — it reverts
`TrackStats` and these rows together, keeping the two halves in sync.

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

**Primary-artist fallback hash.** foobar keeps every featured artist in the
`ARTIST` tag, but fooyin often stores only the lead artist, so a multi-artist
track's full-artist hash misses. `export` therefore emits a *second* hash for
multi-artist records — the same formula with `artists[:1]` (`hash_primary` in the
JSON) — and import tries it right after the full hash. On this library it moves
**258** collab tracks from the path-tail fallback to a content-hash match,
leaving the tail with a negligible residual.

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

### 4.3. Auxiliary — the case-folded hash

The exact hash is over the **raw** strings, so a tag that changed only in
capitalisation produces a completely different hash. That is not hypothetical:
re-downloading an album from another store returned

| foobar's cached title | the new file's title |
|---|---|
| `A Strange Kind **Of** Love` | `A Strange Kind **of** Love` |
| `Bring **On The** Dancing Horses` | `Bring **on the** Dancing Horses` |

— the same recording, a different hash. Tracks on the same album whose titles
contain no lower-cased preposition kept matching, which is what made the failure
look path-related at first: it was not, the folder rename only removed the tail
fallback that would otherwise have caught it.

`core.norm_track_hash` is the same payload with every string passed through
`strip().lower()`. Both sides must recompute it — fooyin stores only the exact
hash — which is why the importer builds a folded index over fooyin's tag columns
(§3.2).

Folding is deliberately limited to case and surrounding whitespace. Folding
further (inner whitespace, Unicode normal forms, punctuation) rescued **no**
additional track on this library while widening the chance that two genuinely
different recordings collapse onto one hash — and that failure is a *silent
wrong write*, which is far worse than a miss. Measured collision cost of the
current fold: **3 of 9731** folded hashes map to more than one `TrackHash`, and
all three are the same recording present in two album editions — the existing
"duplicate copies share one row" case (§5), not a new failure mode.

### 4.4. Resolution order & measured coverage

Per record, import resolves to a fooyin `TrackHash` (`importer._resolve`):

```
full-artist hash in fooyin?  ──yes──▶  that TrackHash        (primary, path-independent)
   └─no─▶ primary-artist hash in fooyin?  ──yes──▶  TrackHash (multi-artist recovery)
      └─no─▶ case-folded hash in fooyin?  ──yes──▶  TrackHash (re-tagged recovery)
         └─no─▶ tail in fooyin Tracks?  ──yes──▶  TrackHash   (last-resort fallback)
            └─no─▶ unmatched (track absent from fooyin)
```

then `TrackHash ──▶ TrackStats`. Measured on this library (foobar **24 788**
raw records, **10 209** after de-duplication (§5), vs fooyin **9 802** tracks):

| Resolution | Count | Note |
|---|---|---|
| by full hash | 10 068 | content hash, path-independent; survives moves / renames / reorganisation |
| by primary-artist hash | 122 | multi-artist tracks fooyin filed under the lead artist only |
| by case-folded hash | 16 | one artist's albums re-downloaded and re-tagged; nothing else could reach them |
| by tail (last resort) | 2 | broken metadata (field-mismatched DSD) the hash can't reproduce |
| unmatched | 1 | not in fooyin at all → correctly skipped |

Content hashes (exact + primary-artist + folded) carry **99.98%** of the tracks
present in both libraries; the path tail is a last-resort net for a handful of
broken-metadata files. Because that tail is anchored on this library's `11.NN`
genre folder, a differently-structured library simply gets those few reported as
unmatched — the hash path, being tag-derived, is unaffected.

Only tail matches are flagged **low-confidence** in the dry-run. A folded-hash
match is still a content match and needs no eyeballing, but its **count** is
reported separately: it is a drift gauge. Sixteen means one album got re-tagged;
three thousand would mean a library-wide re-tag, which is worth knowing before
`--apply`.

---

## 5. Merge semantics

### 5.1. Import (foobar → fooyin) — additive

Existing fooyin rows are merged field-by-field, never blindly overwritten:

| Field | Rule | Idempotent? |
|---|---|---|
| `FirstPlayed` | `min` of non-zero values | ✔ |
| `AddedDate` | `min` of non-zero values | ✔ |
| `LastPlayed` | `max` | ✔ |
| `Rating` | foobar value if rated, else keep fooyin | ✔ |
| `PlayCount` | `current − last_contribution + foobar` | ✔ via sidecar |

`--keep-fooyin-rating` flips the rating precedence: a rating already set in
fooyin is never overwritten, though foobar still fills in the ones fooyin
lacks.

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

Addition is right *here* because the two libraries accumulated plays
independently: foobar's count and fooyin's count are disjoint histories of the
same recording. That premise does not hold for a snapshot of fooyin itself,
which is why `restore` is a separate command with a different rule (§5.4).

### 5.2. De-duplicating stale foobar roots

foobar's `metadb` keys on the **path spelling**, so renaming a library root
leaves the old entries in place: this library holds `D:\11_music` (15 019 rows),
`D:\11_MusicLib` (9 744) and an `exttag_off://` variant — the same files, two to
three times over, with mirrored stats.

Those are not the duplicate copies §5.3 is about. Summing them multiplied every
play count by the number of stale roots (one track read `17` in foobar and `51`
in fooyin). The sidecar could not catch it: it records the *summed* contribution,
so `51 − 51 + 51 = 51` is idempotent and wrong forever. Idempotence preserves
whatever it is given; it does not make it correct.

The discriminator is the album-relative tail (`importer.dedupe_records`):

| Same identity, … | Meaning | Rule |
|---|---|---|
| …same tail | one file, seen under several stale roots | collapse to one record |
| …different tail | a genuine second copy in another folder | keep both, they sum (§5.3) |

Measured: 9 139 groups collapse, 523 are real copies, and **0** groups disagree
on play count — the stale roots are mirrors, so the "which count wins" question
never arises in practice. Where it would, the maximum wins (one history seen
twice, not two histories to add) and the track is listed in the run report.

### 5.3. Duplicate copies

Several library copies of one recording share a single fooyin `TrackHash`
(§3.2), so more than one record can target the same stats row. They are
aggregated by hash *before* the merge above — play counts summed (an unplayed
backup copy adds 0), earliest first/added, latest last, highest rating — and the
row is written once. Without this the records would race to write one row,
giving a non-deterministic, non-idempotent result.

### 5.4. Restore (fooyin → fooyin) — max, not sum

| Field | Rule |
|---|---|
| `FirstPlayed`, `AddedDate` | `min` of non-zero values |
| `LastPlayed` | `max` |
| `Rating` | keep fooyin's if set, else the snapshot's |
| `PlayCount` | **`max(current, snapshot)`** |

`restore` never uses the sidecar arithmetic. Reusing it would mean
`current − contributed + snapshot`, and with a snapshot of the very same
database that is `current + (plays fooyin logged since the last import)` — a
round trip that changes 638 rows and invents 1 277 plays while claiming to
restore. With `max`, a snapshot taken and restored with nothing changed in
between is exactly a no-op, which is the property the command is tested against.

`max` costs the plays that landed on the *new* row between the file edit and the
repair. That is a small, bounded loss (the repair follows the edit closely) and
buys idempotence, which matters more for a rescue command that gets re-run while
its dry-run output is being deciphered.

**The sidecar is carried across.** When stats move from hash *A* to hash *B*,
`_fb2fooyin_import[A]` moves too (never overwriting an entry *B* already has). If
it did not, a later foobar import would see no prior contribution on *B* and add
its whole count on top of the restored one. The cost is one field in the JSON
and one `INSERT OR IGNORE`; the cost of omitting it is a silent double-count
years later, with nothing left to compare against.

---

## 6. The fooyin-side loop — `snapshot` / `restore` / `orphans`

### 6.1. Why foobar cannot be the answer

Matching against foobar only works while foobar still knows the track. Once it
is retired, that premise decays: measured over the 27 days after the migration,
**638** tracks accumulated **1 277** plays that exist only in fooyin, and **64**
tracks were added that foobar has never seen at all. For those 64, no import can
help — there is nothing on the other side to match against.

So the durable failure mode is not "foobar and fooyin disagree", it is "fooyin
lost track of its own recording". That needs a fooyin-side loop.

### 6.2. `snapshot` — leave evidence before the change

`Tracks` has no soft delete (§3.2). The moment a file's tags change, the
`FilePath → TrackHash → tags` mapping is gone, leaving a stats row keyed by a
hash that can no longer be derived from anything. **A hash cannot be reversed
into tags**, so nothing after the fact can repair it.

`snapshot` is therefore not a backup, it is *evidence*: one read of `Tracks ⋈
TrackStats` recording each row's exact hash, folded hash, path, track number,
title, and the sidecar contribution. Its only requirement is timing — it must
exist **before** the edit. `--path` narrows it to one directory, but the default
is the whole library precisely because the scoped form assumes you know in
advance what you are about to break, and the incident that motivated this design
was noticed weeks late.

Orphans cannot be snapshotted (no tags, nothing to fold), so the command
protects what is currently healthy and nothing else.

### 6.3. `restore` — put the stats back on the current identity

```
hash still in Tracks?            ──yes──▶ skip — the identity never broke
  └─no─▶ folded hash matches?    ──yes──▶ move   (tags re-cased)
      └─no─▶ same FilePath?      ──yes──▶ move   (re-tagged in place)  ⚠
          └─no─▶ track no. + folded title, within --to?  ──▶ move      ⚠
              └─no─▶ unresolved
```

The first two layers are deterministic. The `FilePath` layer covers the case the
folded hash misses — tags rewritten *without* renaming — and is the mirror image
of the folded hash: one survives a rename, the other survives a re-tag.

The last layer is a heuristic and is gated twice: it needs an explicit `--to`
directory, and the record set must be under `--fuzzy-limit` (100). The scope is
the part that matters — "track 1 / Intro" is unique within an album and
worthless across a library. A record-count limit alone would not bound the
*candidate* set, only the query set.

### 6.4. `orphans` — the only alarm

A stats row whose hash is not in `Tracks`. Every broken identity lands here, so a
non-empty list means something changed identity without being repaired. This is
how the original incident should have been noticed: 16 tracks across four albums
had been stranded for weeks, and nothing said so.

Read-only by default. `--from-json` re-attaches a human-readable path by looking
each hash up in a snapshot or export. Rows with `PlayCount = 0` are hidden
unless `--all` — they hold no data and no information, and only dilute the
signal.

**Pruning.** `restore --prune-moved` deletes a source row once its stats have
been written elsewhere — completing a move rather than leaving a copy — and only
for deterministic matches; a heuristic match keeps its source row, because
"someone glanced at the dry-run" is not a licence to destroy the last copy.
`orphans --prune` is the unscoped sweep, and exists to keep the alarm meaningful:
a list that still contains everything ever repaired stops working as a signal.
Both default to off and require `--apply`.

The unscoped sweep is the riskiest operation in the tool — an unmounted drive
makes an entire library look like orphans — so it refuses to run while any
`Libraries.Path` is absent from disk.

---

## 7. Safety model

- Every writing command is **dry-run by default**; `--apply` is required. Dry
  runs open fooyin **read-only** and print the change plan (insert/update
  counts, unmatched list, sample diffs).
- Changes resolved only by path tail (`import`) or by the heuristic layer
  (`restore`) are listed as **low-confidence**, so the handful of guesses can be
  eyeballed before `--apply`.
- `--apply` copies `fooyin.db` → `fooyin.db.bak-<timestamp>` before any write.
- To **revert** an import, restore its `fooyin.db.bak-<timestamp>` — this rolls
  back the merged `TrackStats` and the `_fb2fooyin_import` rows together. Deleting
  the sidecar by hand instead desyncs the play-count ledger and double-counts on
  the next import (§3.4).
- `--apply` issues `BEGIN IMMEDIATE` (`importer.begin_write`); if fooyin holds
  the lock it aborts with a "close fooyin first" error rather than risk a
  corrupt/partial write.
- Deletion is never a side effect: `--prune-moved` and `--prune` are separate,
  default-off switches, and the unscoped one additionally refuses while a
  library root is missing from disk (§6.4).
- A payload is tagged with its producer (`kind`); feeding a snapshot to `import`
  or an export to `restore` fails loudly instead of applying the wrong merge
  rule.
- All writes commit in a single transaction.

---

## 8. Boundaries & non-goals

- **No audio-file reading.** Tags come from foobar's `metadb.info` cache
  (§2.5) or fooyin's own columns (§3.2), never by opening the audio files — so
  export works for files offloaded to cloud storage.
- **Hash reproduction, not audio hashing.** fooyin's `TrackHash` is recomputed
  from tags (§4.1); the tool never hashes audio content.
- **No fuzzy field matching.** Tag columns are read only to recompute hashes
  (§3.2). The one heuristic layer that compares text (`restore`'s track number +
  title) is scoped to a directory, capped, off by default, and never permitted
  to delete its source.
- **`snapshot` cannot recover the past.** It protects rows that are healthy when
  it runs; a break that happened before the first snapshot is only repairable
  from foobar, and only while foobar still knows the track (§6.1).
- **CUE / subsongs.** `subsong` is part of the reproduced hash (`str(subsong)`),
  so multi-subsong tracks would hash correctly — but this library has none
  (`Subsong` is always 0), so it is untested.
- **One direction only** (foobar → fooyin). There is no fooyin → foobar path.
- The Playback Statistics GUID is **auto-detected** (`core.detect_stats_guid`, by
  the uniform-40-byte `_data` signature), so a different foobar profile works
  without code edits; `core.STATS_INDEX_GUID` is only the fallback default.
