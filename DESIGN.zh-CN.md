# DESIGN — fb2fooyin（设计文档）

[English](DESIGN.md) · **简体中文**

将 foobar2000 的播放统计迁移到 fooyin。本文是**两个数据库各自存了什么、存在哪、
工具如何读写**的权威记录。实现位于 `fb2fooyin/`；本文解释背后的*原因*与磁盘上的
数据契约。

---

## 1. 目标与第一性原理

本工具本质上是围绕一个中间文件的两个纯变换：

```
foobar2000 metadb.sqlite  ──export──▶  stats.json  ──import──▶  fooyin.db
        （只读）                       （可移植）              （事务写入）
```

- **export**＝把 foobar 藏在不透明 BLOB 里的统计读出来，再从 foobar 的标签缓存里
  读出每曲的标签，一起归一化成稳定、可读的键值形式 —— 其中包含**复刻出的
  fooyin `TrackHash`**。
- **import**＝把这些记录匹配到 fooyin 曲目 —— 先按内容 hash、再以路径尾部兜底 ——
  并将 5 个字段合并进 fooyin 的统计表。

中间 JSON 每条记录携带两个*内容稳定*的标识：复刻出的 fooyin **`TrackHash`**
（主键 —— 标签的哈希，对任何路径变更免疫）与**专辑相对路径尾部**（兜底 —— 给少数
无法复刻哈希的曲目）。两者都不是数据库 rowid 或绝对路径，从而在音乐库重组后仍然
有效，也让两个阶段彼此解耦。

迁移的 5 个字段：**播放次数、首次播放、最近播放、添加时间、评分**。

---

## 2. foobar2000 侧 —— `metadb.sqlite`

由 `foo_playcount` 组件写入。它**不是**可直接查询的关系型 schema，而是每曲一个
不透明 BLOB 的键值存储。

### 2.1. 表

| 表 | 列 | 作用 |
|---|---|---|
| `config` | `key TEXT UNIQUE`、`value TEXT` | 组件版本标志（`version`、`oldRatingsFixed`、`uniNormFix`） |
| `metadb` | `name TEXT PK`、`info BLOB`、`infoBrowse BLOB`、`size`、`lastModified`、`infoBrowseTime`、`lastseen`、`created`、`attribs`、`attribsValid` | 每文件的标签/技术信息缓存；**`info` 被读取取标签**以复刻 fooyin 哈希（§2.5、§4） |
| `metadb_indexes` | `name TEXT PK`、`synced INTEGER`、`retention INTEGER` | 下列各组件索引的注册表 |
| `metadb_index_<GUID>` | `key INTEGER`、`filename TEXT UNIQUE PK` | 整数 key ⇄ 曲目位置字符串 的映射 |
| `metadb_index_<GUID>_data` | `key INTEGER PK UNIQUE`、`value BLOB` | 每曲负载，按 `key` 与名字表关联 |

每个 foobar 索引都是这样**成对的两张表** —— 一张 `_<GUID>` 名字表和一张
`_<GUID>_data` 负载表，按 `key` 关联。

### 2.2. 我们读取的索引

本库的 `metadb_indexes` 注册了 7 个 GUID，只有一个携带我们要的播放统计：

| GUID | 工具表 | 判定为 | 使用 |
|---|---|---|---|
| **`C653739F-14B3-4EF2-819B-A3E2883230AE`** | `metadb_index_C653739F_14B3_4EF2_819B_A3E2883230AE`（+ `_data`） | **播放统计**（次数/首播/末播/添加/评分） | **✔ 是** |
| `0C1BD000-…-48EE4249DEC3` | 同样成对 | 逐次播放的时间戳历史（变长 FILETIME 数组） | ✘ |
| `0C1BD000-…-48EE4249DED0` | 同样成对 | 20 字节记录，未识别 | ✘ |
| `EF148A2E-…`、`0FEBCD4A-…`、`E58A4298-…`、`7D07305A-…` | 同样成对 | 未识别 / `_data` 为空 | ✘ |

> 在 SQLite 中 GUID 的连字符在真实表名里变成下划线。工具以下划线形式硬编码在
> `core.STATS_INDEX_GUID`。

### 2.3. `filename`（名字表的键）

格式：`"<子歌曲>+<位置>"`，例如：

```
0+file://D:\11_MusicLib\11.11_C-Pop\Fine乐团\2015 I’m Sorry [16-44-WAV]\01. …wav
```

- `0+` 是子歌曲索引前缀。本库没有 CUE，因此恒为 `0+`。
- 位置是 foobar 的 URI，带**Windows**反斜杠与盘符，是迁移到 Linux 之前采集的。
- 非文件行（电台流 `http://…`、`unpack://zip|…`）也在此表内，因不含流派目录而被
  跳过（见 §4）。

### 2.4. `value` BLOB 布局（播放统计）

固定 **40 字节**，小端。已对真实数据核对（`core.parse_stats_blob`）：

| 偏移 | 长度 | 类型 | 字段 |
|---|---|---|---|
| `0` | 4 | `uint32` | **播放次数** |
| `4` | 4 | — | 零填充 |
| `8` | 8 | `FILETIME` | **首次播放** |
| `16` | 8 | `FILETIME` | **最近播放** |
| `24` | 8 | `FILETIME` | **添加时间** |
| `32` | 1 | `uint8` | **评分**（`0xFF` = 未评分） |
| `33` | 7 | — | 零填充 |

- **FILETIME**＝自 1601-01-01 起的 100 纳秒计数。`0` 表示“从未”。
- **评分字节**采用近似线性（间距约 42.5）的编码。锚点取自真实库：

  | 字节 | `0x3F` | `0x6A` | `0x95` | `0xBF` | `0xEA` | `0xFF` |
  |---|---|---|---|---|---|---|
  | 星级 | 1 | 2* | 3 | 4 | 5 | 未评分 |

  `*` 2★（`0x6A`）是外推值 —— 本库不存在 2★ 曲目。解码采用最近锚点匹配，因此
  即便字节偏离锚点一格仍能正确解析。

### 2.5. `metadb.info` BLOB 布局（标签）

`metadb` 表的 `info` BLOB 是 foobar 每文件的标签缓存。工具读取它以复刻 fooyin
哈希（§4）。按 `metadb.name = metadb_index_<GUID>.filename` 与统计索引关联
（**26201/26201 行精确关联**）。

在一段二进制头（replaygain 浮点、MusicBrainz id…）之后，标签是以 NUL 分隔的
token，排布为 **`KEY \0 值 [\0 值 …] \0`** 的分组（空 token 终结一组；一个键可有
多个值，如两个 `ARTIST`）。两个坑，均已跨格式核实，由 `core.parse_info_tags`
处理：

- **键名大小写随源格式变。** FLAC/Vorbis 键为大写（`TITLE`、`TRACKNUMBER`）；
  MP4/m4a 键为小写（`title`、`tracknumber`）。故键名**大小写不敏感**匹配。
- **首个标签（`ALBUM`）粘在二进制头上**，前面没有 NUL，从不作为干净 token 出现。
  靠其唯一后缀 `ALBUM` 识别（`ALBUM ARTIST` / `ALBUMARTISTSORT` 都不以它结尾）。

无标签的抓轨（部分 WAV）读不出标签 token → 字段全空 → 该记录哈希失配，回退到
尾部（§4）。

---

## 3. fooyin 侧 —— `fooyin.db`

位置：`~/.local/share/fooyin/fooyin.db`。正常的关系型 schema。

### 3.1. 表（相关子集）

| 表 | 作用 |
|---|---|
| `Libraries` | 库根路径 —— 此处 `id=2, name=11_music, path=/home/xre/11_music` |
| `Tracks` | 每个音频文件一行（标签+技术信息+hash） |
| `TrackStats` | 播放统计，**以 `TrackHash` 为主键** —— 写入目标 |
| `Playlists`、`PlaylistTracks`、`PlaybackQueue`、`Settings`、`TracksView` | 无关 |

### 3.2. `Tracks`（只读 —— 用于反查 hash）

| 列 | 类型 | 说明 |
|---|---|---|
| `TrackID` | INTEGER PK AUTOINCREMENT | |
| `FilePath` | TEXT NOT NULL | 绝对 Linux 路径，流派目录小写，如 `/home/xre/11_music/11.11_c-pop/…` |
| `Subsong` | INTEGER DEFAULT 0 | 本库恒为 0 |
| `TrackHash` | TEXT | **内容型** hash（艺人/专辑/标题…），**不**由路径导出。工具从 foobar 的标签**复刻**此哈希并据此匹配（§4） |
| *（大量标签/技术列）* | | `Title`、`Artists`、`Album`… —— fooyin 侧**不读取**；哈希是从 *foobar* 的标签复刻后与 `TrackHash` 比对 |

关键性质（已验证）：**同一个 `TrackHash` 出现在多个不同 `FilePath`**（专辑重复
副本），证明 hash 由元数据导出。约束 `UNIQUE(FilePath, Offset, Subsong)`。

### 3.3. `TrackStats`（写入目标）

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

| 列 | 单位 / 约定 | 工具是否写 |
|---|---|---|
| `TrackHash` | 主键，来自 `Tracks` | 关联键 |
| `LastSeen` | Unix 毫秒 | 不动（fooyin 自有） |
| `AddedDate` | Unix 毫秒 | ✔ 合并（取更早） |
| `FirstPlayed` | Unix 毫秒（`0`＝从未） | ✔ 合并（取更早） |
| `LastPlayed` | Unix 毫秒（`0`＝从未） | ✔ 合并（取更晚） |
| `PlayCount` | 整数 | ✔ 合并（幂等相加） |
| `Rating` | REAL `星/5`；`-1.0`＝未评分 | ✔ 合并（foobar 已评分时以其为准） |

印证约定的实测值：`Rating` 为 `0.4`（=2★）、`0.8`（=4★）、`-1.0`（未评分）；
时间戳为 13 位 Unix **毫秒**。

### 3.4. 工具创建的旁路表

```sql
CREATE TABLE _fb2fooyin_import (
    TrackHash            TEXT PRIMARY KEY,
    ContributedPlayCount INTEGER NOT NULL,
    ImportedAt           INTEGER NOT NULL   -- Unix 毫秒
);
```

记录*本工具*上一次为每个 hash 贡献了多少播放数，从而让 `PlayCount` 的相加合并在
重跑时保持幂等（见 §5）。fooyin 会忽略它不认识的表；该表随备份/恢复一起迁移。

---

## 4. 匹配键 —— 复刻的 fooyin `TrackHash`，路径尾部兜底

fooyin 用**标签的内容哈希**（而非路径）标识一段录音，因此该哈希对文件移动/改名
免疫。从 foobar 缓存的标签复刻出这个哈希，就能按身份匹配；专辑相对路径尾部仅作为
少数无法复刻哈希曲目的兜底。

### 4.1. 主键 —— 复刻的哈希

已对照 fooyin 源码核实（`src/core/track.cpp` `Track::generateHash` +
`include/utils/crypto.h` `Utils::generateHash`）：该哈希是下列各项按顺序做 UTF-8
拼接（**无分隔符**）后的小写十六进制 **MD5**：

```
artists.join(",")  ++  album  ++  discNumber  ++  trackNumber  ++  title  ++  str(subsong)
```

—— 用**原始标签串**，不做大小写归一。`core.fooyin_track_hash` 复刻它；
`core.parse_info_tags` 从 foobar 的 `metadb.info`（§2.5）提供字段；subsong 取自
`N+` 文件名前缀（§2.3）。

用 fooyin 自身存储的字段对活库 `fooyin.db` 复刻，**100%（9740/9740）**吻合 ——
即算法精确。

> 边界：当 title 为空时 fooyin 会回退成 `目录 + 文件名`。这无法从 foobar 的
> Windows 路径复刻，因此无标题曲目会哈希失配、回退到尾部。

### 4.2. 兜底 —— 专辑相对路径尾部

留给哈希复刻不了的残差（§4.3）。两个库在专辑*以上*的一切都不同：

| | foobar | fooyin |
|---|---|---|
| 根 | `D:\11_MusicLib\` | `/home/xre/11_music/` |
| 分隔符 | `\` | `/` |
| 流派目录大小写 | `11.11_C-Pop` | `11.11_c-pop` |

……但流派目录**以下** —— `艺人/专辑/文件名` —— **逐字节一致**（专辑目录与文件名
从未改名，只有上层目录在 Windows→Linux 迁移时被改成小写）。

因此匹配键即为**流派段 `11.NN_…` 之后、忽略大小写的尾部**：

```
0+file://D:\11_MusicLib\11.11_C-Pop\Fine乐团\Album\01. Song.wav
/home/xre/11_music/11.11_c-pop/Fine乐团/Album/01. Song.wav
                    └────────────────┬──────────────────────┘
        两者 →  fine乐团/album/01. song.wav
```

实现于 `core.path_tail`：剥掉 `N+` 子歌曲前缀、去掉 `file://`、归一化分隔符、
转小写，再取流派目录 `/11.\d\d…/` 之后的部分。不含流派目录的路径（电台、
zip 内嵌）返回 `None` —— 这类记录仍可按哈希匹配。

### 4.3. 解析顺序与实测覆盖

每条记录，导入按此解析到 fooyin `TrackHash`（`importer._resolve`）：

```
哈希在 fooyin 里？ ──是──▶  该 TrackHash          （主键，与路径无关）
       └─否─▶  尾部在 fooyin Tracks 里？ ──是──▶  TrackHash   （兜底）
                     └─否─▶  未匹配（fooyin 里根本不存在）
```

然后 `TrackHash ──▶ TrackStats`。在本库实测（foobar **25 175** 条记录 vs
fooyin **9 740** 曲）：

| 解析方式 | 数量 | 说明 |
|---|---|---|
| 按哈希 | 24 909 | 与路径无关；**额外救回约 1 134** 首尾部匹配不到的（移动/改名/重组） |
| 按尾部（兜底） | 238 | 全是多艺人 m4a —— foobar 保留了 featured 艺人、fooyin 只存主艺人，故复刻的哈希不同 |
| 未匹配 | 28 | fooyin 里根本不存在（已删专辑、电台）→ 正确跳过 |

对两个库都存在的曲目，哈希+尾部合并覆盖：**100%，尾部零歧义。** 哈希与尾部互补
—— 哈希能扛住尾部扛不住的改名，尾部能覆盖哈希覆盖不了的标签解析残差。

---

## 5. 合并语义（导入）

对 fooyin 已有行逐字段合并，绝不盲目覆盖：

| 字段 | 规则 | 幂等？ |
|---|---|---|
| `FirstPlayed` | 非零值取 `min` | ✔ |
| `AddedDate` | 非零值取 `min` | ✔ |
| `LastPlayed` | 取 `max` | ✔ |
| `Rating` | foobar 已评分则用之，否则保留 fooyin | ✔ |

`--keep-fooyin-rating` 会翻转评分优先级：fooyin 里已有的评分绝不被覆盖，但 foobar
仍会补上 fooyin 缺的那些。
| `PlayCount` | `现值 − 上次贡献 + foobar` | ✔ 借助旁路表 |

`PlayCount` 是唯一不平凡的规则。朴素相加会在重跑时双计；朴素覆盖会丢掉 fooyin
自己记录的播放。旁路表存下每次贡献，于是：

```
新值 = 现有播放数 − 上次贡献 + foobar 播放数
```

- 首次：`贡献 = 0` → 加上完整 foobar 计数；
- 重跑、foobar 未变：加 `0`（幂等）；
- foobar 增长了 N：正好加 N；
- 期间你在 fooyin 里播放过：该增量得以保留。

foobar 时间戳为 0 视作“未知”，绝不覆盖 fooyin 的真实值。

**重复副本。** 同一录音在库中的多份拷贝共享同一个 fooyin `TrackHash`（§3.2），
因此多条导出记录可能指向同一行统计。它们会在上述合并*之前*先按 hash 聚合 ——
播放数相加（未播过的备份副本加 0）、first/added 取最早、last 取最晚、评分取最高 ——
然后只写一次。否则这些记录会竞相写同一行，产生非确定、非幂等的结果。

---

## 6. 安全模型

- 导入**默认 dry-run**，必须加 `--apply` 才写入。dry-run 以**只读**打开 fooyin
  并打印变更计划（插入/更新计数、未匹配清单、样例 diff）。
- `--apply` 在任何写入前把 `fooyin.db` 复制为 `fooyin.db.bak-<时间戳>`。
- `--apply` 执行 `BEGIN IMMEDIATE`；若 fooyin 持有锁则以“请先关闭 fooyin”报错
  中止，而非冒着写坏/写一半的风险。
- 所有写入在单个事务内提交。

---

## 7. 边界与非目标

- **不读音频文件。** 标签来自 foobar 的 `metadb.info` 缓存（§2.5），从不打开音频
  文件 —— 因此导出只依赖 metadb，对已转移到网盘的文件也照常工作。
- **复刻哈希，而非音频哈希。** fooyin 的 `TrackHash` 是从标签重算的（§4.1），工具
  从不对音频内容做哈希。路径尾部匹配保留为兜底（§4.2）。
- **CUE / 子歌曲。** `subsong` 是复刻哈希的一部分（`str(subsong)`），故多子歌曲曲目
  能正确哈希 —— 但本库没有（`Subsong` 恒为 0），因此未经测试。
- **单向**（foobar → fooyin），没有 fooyin → foobar 的反向路径。
- 播放统计 GUID 被当作本库 `metadb.sqlite` 的固定常量；不同的 foobar 配置可能使用
  不同 GUID，届时需更新 `core.STATS_INDEX_GUID`。
