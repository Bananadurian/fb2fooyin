# DESIGN — fb2fooyin（设计文档）

[English](DESIGN.md) · **简体中文**

将 foobar2000 的播放统计迁移到 fooyin。本文是**两个数据库各自存了什么、存在哪、
工具如何读写**的权威记录。实现位于 `fb2fooyin/`；本文解释背后的*原因*与磁盘上的
数据契约。

---

## 1. 目标与第一性原理

本工具是围绕**一种中间文件格式**的一组纯变换：

```
foobar2000 metadb.sqlite  ──export────▶  stats.json    ──import───▶  fooyin.db
        （只读）                                                    （事务写入）

fooyin.db  ──snapshot──▶  snapshot.json  ──restore──▶  fooyin.db
 （只读）                                              （事务写入）
```

- **export**＝把 foobar 藏在不透明 BLOB 里的统计读出来，再从 foobar 的标签缓存里
  读出每曲的标签，一起归一化成稳定、可读的键值形式 —— 其中包含**复刻出的
  fooyin `TrackHash`**。
- **import**＝把这些记录匹配到 fooyin 曲目 —— 先按内容 hash、再以路径尾部兜底 ——
  并将 5 个字段合并进 fooyin 的统计表。
- **snapshot**＝同一套记录格式，但由 fooyin 自己产出。
- **restore**＝把快照里的统计放回这段录音**当前**的身份上，修复快照之后断裂的身份。

因为 JSON 是一种**格式**而非一条通道，生产者可以替换：同一套消费逻辑既服务于一个
外部库（foobar），也服务于本库的过去（快照）。每份 payload 都标注了 `kind`，避免
喂错消费者 —— 二者共享记录 schema，但**不共享合并语义**（§5）。

每条记录携带的都是*内容稳定*的标识，而非 rowid 或绝对路径，因此音乐库重组后依然
有效，两个阶段也彼此解耦：

| 标识 | 能扛住 | 会失效于 |
|---|---|---|
| `hash` —— 复刻的 fooyin `TrackHash` | 移动、改名、整个重组 | 标签发生变化 |
| `hash_primary` —— 同上，只取主艺人 | fooyin 把 collab 只归到主艺人名下 | — |
| `hash_norm` —— 同上，大小写归一化 | 重下载/重打标签导致的大小写漂移 | 用词本身改了 |
| `tail` / `file_path` —— 路径 | 原地重打标签 | 目录被改名 |

最后两行是本设计被重写的原因：哈希与路径在**相反**的条件下失效，所以两者并存才让
恢复成为可能。要丢掉一张专辑的历史，必须同时打断两者 —— 而「把专辑重下到一个改了
名的目录里」恰好就是这么做的。

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

> 在 SQLite 中 GUID 的连字符在真实表名里变成下划线。`core.detect_stats_guid`
> 自动识别它 —— 选出唯一那张「非空、且 `_data` blob 长度**全部**恰好 40 字节」的
> 索引（上面那个历史索引也含少量 40 字节行，故判别式是「全 40」而非「有 40」）。
> 下划线形常量 `core.STATS_INDEX_GUID` 仅作检测失败时的兜底；`export` 会把识别到的
> GUID 写进 JSON（`stats_index_guid`）。

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
| `Title`、`Artists`、`Album`、`DiscNumber`、`TrackNumber` | TEXT | **仅**用于重算哈希 —— 见下方边界说明 |

`Artists` 是用单元分隔符 `\x1f` 连接的列表。

关键性质（已验证）：**同一个 `TrackHash` 出现在多个不同 `FilePath`**（专辑重复
副本），证明 hash 由元数据导出。约束 `UNIQUE(FilePath, Offset, Subsong)`。

> **边界 —— 读取 fooyin 的标签列。** 这些列被读取只有一个用途：重算哈希
> （`core.read_fooyin_tracks`、`core.build_norm_index`）。**绝不**在两个库之间
> 比对标签*值*。这个区分很重要：哈希相等是确定性的身份判定，而比对字段是模糊匹配
> —— 本工具不做模糊匹配（§8）。已验证：用这些列重算 `fooyin_track_hash` 对全库
> **9802/9802** 条存储哈希完全命中，因此基于它们构建的归一化变体是精确的，而非
> 近似的。

无软删除。文件标签一旦变化，fooyin 会丢弃旧的 `Tracks` 行、以新哈希插入新行 ——
旧身份连同标签一起从数据库消失。它的 `TrackStats` 行则作为**孤儿**留存，其主键是
一个再也无法从任何东西推导出来的哈希（§6）。

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

> fooyin 的评分**刻度**设置（`0-1` / `1-5` / `1-10` / `1-100`）只影响显示与文件标签
> 读写（`src/core/engine/input/ratingtagpolicy.cpp`）；`TrackStats.Rating` 永远是这个
> 归一化 `0.0–1.0` 浮点（`Track::rating()`），故工具按 `星/5` 写入与刻度无关。

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

它在**首次 `--apply`** 时才创建（`export`、`inspect`、dry-run 都不会创建）。请把
它当作**收据而非缓存**：它是那笔播放数账目里存在 `TrackStats` 之外的另一半。手动
删除它并不能撤销任何东西 —— 只会丢掉「回退上次贡献」这一步，导致下次导入双计。以
fooyin 现值 `5`、foobar `10` 为例：

| 运行 | `PlayCount` 合并 | 结果 |
|---|---|---|
| 首次 `--apply` | `5 − 0 + 10` | `15` |
| 重跑，sidecar 在 | `15 − 10 + 10` | `15`（幂等） |
| 重跑，删了 sidecar | `15 − 0 + 10` | `25`（双计） |

要干净地重做或撤销一次导入，请改为恢复备份（§6）—— 它会把 `TrackStats` 与这些行
一并还原，让两半保持同步。

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

**主艺人兜底哈希。** foobar 在 `ARTIST` 标签里保留所有 featured 艺人，而 fooyin
往往只存主艺人，因此多艺人曲目的「全艺人哈希」会失配。故 `export` 为多艺人记录再
算一个哈希 —— 同一公式但用 `artists[:1]`（JSON 里的 `hash_primary`）—— 导入时紧接
全艺人哈希之后尝试。本库借此把 **258** 首 collab 从路径尾部兜底升级为内容哈希匹配，
尾部只剩可忽略的残差。

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

### 4.3. 辅助 —— 大小写归一化哈希

精确哈希基于**原始**字符串，因此只改了大小写的标签会算出完全不同的哈希。这并非
假设：从另一家商店重下一张专辑后得到的是

| foobar 缓存的标题 | 新文件的标题 |
|---|---|
| `A Strange Kind **Of** Love` | `A Strange Kind **of** Love` |
| `Bring **On The** Dancing Horses` | `Bring **on the** Dancing Horses` |

—— 同一段录音，不同的哈希。同专辑里标题不含小写介词的曲目照常命中，这正是最初
让人误以为问题出在路径上的原因：并不是，目录改名只是顺带把本可兜住它的尾部兜底
也一起废掉了。

`core.norm_track_hash` 是同一套 payload，但每个字符串先过 `strip().lower()`。
两侧都必须重算 —— fooyin 只存了精确哈希 —— 因此导入阶段会基于 fooyin 的标签列
构建归一化索引（§3.2）。

归一化被刻意限制在大小写与首尾空白。再往下折叠（内部空格、Unicode 规范形式、标点）
在本库**救不回任何**额外曲目，却扩大了「两段真正不同的录音塌缩到同一个哈希」的
概率 —— 而那种失败是**静默写错**，远比漏匹配糟糕。当前折叠的实测碰撞代价：
**9731 个归一化哈希中有 3 个**对应多于一个 `TrackHash`，且三者都是同一录音在两个
专辑版本中的副本 —— 属于既有的「重复副本共享一行」情形（§5），并非新的失败模式。

### 4.4. 解析顺序与实测覆盖

每条记录，导入按此解析到 fooyin `TrackHash`（`importer._resolve`）：

```
全艺人哈希在 fooyin 里？ ──是──▶  该 TrackHash        （主键，与路径无关）
   └─否─▶ 主艺人哈希在 fooyin 里？ ──是──▶  TrackHash  （多艺人救回）
      └─否─▶ 归一化哈希在 fooyin 里？ ──是──▶ TrackHash （重打标签救回）
         └─否─▶ 尾部在 fooyin Tracks 里？ ──是──▶ TrackHash （最后兜底）
            └─否─▶ 未匹配（fooyin 里根本不存在）
```

然后 `TrackHash ──▶ TrackStats`。在本库实测（foobar **24 788** 条原始记录、去重后
**10 209** 条（§5），对 fooyin **9 802** 曲）：

| 解析方式 | 数量 | 说明 |
|---|---|---|
| 按全艺人哈希 | 10 068 | 内容哈希，与路径无关；扛得住移动/改名/重组 |
| 按主艺人哈希 | 122 | fooyin 只按主艺人归档的多艺人曲目 |
| 按归一化哈希 | 16 | 某位艺人的专辑被重下并重打了标签；别无他法可及 |
| 按尾部（最后兜底） | 2 | 哈希复刻不了的坏元数据（字段对不齐的 DSD） |
| 未匹配 | 1 | fooyin 里根本不存在 → 正确跳过 |

内容哈希（精确 + 主艺人 + 归一化）覆盖两库共有曲目的 **99.98%**；路径尾部只是给
少数坏元数据文件的最后一张网。由于该尾部锚定本库的 `11.NN` 流派目录，换到结构不同
的库时，这几首只会被报为「未匹配」—— 基于标签的哈希路径不受影响。

只有尾部匹配会被标为**低置信度**。归一化哈希命中仍属内容匹配，无需人工过目，但它的
**数量**会单独报出：这是一个漂移计量表。16 意味着某张专辑被重打了标签；3000 则意味着
全库级的重打标签，那是 `--apply` 之前值得先知道的事。

---

## 5. 合并语义

### 5.1. 导入（foobar → fooyin）—— 相加

对 fooyin 已有行逐字段合并，绝不盲目覆盖：

| 字段 | 规则 | 幂等？ |
|---|---|---|
| `FirstPlayed` | 非零值取 `min` | ✔ |
| `AddedDate` | 非零值取 `min` | ✔ |
| `LastPlayed` | 取 `max` | ✔ |
| `Rating` | foobar 已评分则用之，否则保留 fooyin | ✔ |
| `PlayCount` | `现值 − 上次贡献 + foobar` | ✔ 借助旁路表 |

`--keep-fooyin-rating` 会翻转评分优先级：fooyin 里已有的评分绝不被覆盖，但 foobar
仍会补上 fooyin 缺的那些。

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

相加在**这里**是对的，因为两个库各自独立累积：foobar 的计数与 fooyin 的计数是同一
录音的两段互不相交的历史。这个前提对「fooyin 自己的快照」不成立，所以 `restore`
是一条独立命令、用另一套规则（§5.4）。

### 5.2. 折叠 foobar 的残留根路径

foobar 的 `metadb` 以**路径写法**为键，因此改过媒体库根目录后旧条目仍然留在原地：
本库同时存有 `D:\11_music`（15 019 行）、`D:\11_MusicLib`（9 744 行）以及一个
`exttag_off://` 变体 —— 同一批文件重复了两到三份，统计数据互为镜像。

它们不是 §5.3 所说的重复副本。把它们相加，会让每首歌的播放数乘上残留根目录的份数
（某曲在 foobar 里是 `17`，在 fooyin 里却成了 `51`）。旁路表救不了：它记录的是**求和
后**的贡献，于是 `51 − 51 + 51 = 51`，幂等且永远错。幂等只保住它被给到的东西，并不
使之正确。

判据是专辑相对尾部（`importer.dedupe_records`）：

| 同一身份，且… | 含义 | 规则 |
|---|---|---|
| …尾部相同 | 同一个文件，在多个残留根下被看见 | 折叠成一条 |
| …尾部不同 | 另一个目录里的真副本 | 都保留，相加（§5.3） |

实测：9 139 组被折叠，523 组是真副本，**0** 组在播放数上打架 —— 残留根互为镜像，
所以「取哪个计数」的问题在实践中根本不会出现。真出现时取最大值（同一段历史被看见
两次，而非两段历史相加），并在运行报告里列出该曲。

### 5.3. 重复副本

同一录音在库中的多份拷贝共享同一个 fooyin `TrackHash`（§3.2），因此多条记录可能
指向同一行统计。它们会在上述合并*之前*先按 hash 聚合 —— 播放数相加（未播过的备份
副本加 0）、first/added 取最早、last 取最晚、评分取最高 —— 然后只写一次。否则这些
记录会竞相写同一行，产生非确定、非幂等的结果。

### 5.4. 还原（fooyin → fooyin）—— 取 max，不相加

| 字段 | 规则 |
|---|---|
| `FirstPlayed`、`AddedDate` | 非零值取 `min` |
| `LastPlayed` | 取 `max` |
| `Rating` | fooyin 有则保留，否则用快照的 |
| `PlayCount` | **`max(现值, 快照值)`** |

`restore` 绝不使用旁路表那套算术。若沿用，就是 `现值 − 贡献 + 快照值`，而当快照取自
同一个数据库时，这等于 `现值 +（上次导入以来 fooyin 记录的播放）` —— 一次自称「还原」
的往返会改动 638 行、凭空造出 1 277 次播放。改用 `max` 后，「拍下快照、其间什么都不
改、再还原」严格等于空操作，这正是该命令被测试锁定的性质。

`max` 的代价是丢掉「文件改动 → 修复」之间落在**新行**上的那几次播放。这是一个很小
且有界的损失（修复通常紧随改动），换来的是幂等 —— 对一条你会一边琢磨 dry-run 输出
一边重跑的救援命令来说，幂等更重要。

**旁路表会一并迁移。** 统计从哈希 *A* 搬到哈希 *B* 时，`_fb2fooyin_import[A]` 也跟着
搬（绝不覆盖 *B* 已有的条目）。若不搬，日后再跑 foobar 导入时它会认为 *B* 上没有过
贡献，于是把整份计数加到已还原的数值之上。代价是 JSON 里多一个字段、一条
`INSERT OR IGNORE`；省掉它的代价则是若干年后一次静默的双计，且那时已无参照物可比。

---

## 6. fooyin 侧回路 —— `snapshot` / `restore` / `orphans`

### 6.1. 为什么 foobar 不能是答案

靠 foobar 匹配，只在 foobar 仍然认识这首曲子时有效。它一旦退役，这个前提就开始烂掉：
实测在迁移后的 27 天里，**638** 首曲目累积了 **1 277** 次只存在于 fooyin 的播放，另有
**64** 首曲目是 foobar 从未见过的。对这 64 首，任何导入都无能为力 —— 对面根本没有可
匹配的东西。

所以真正持久的失败模式不是「foobar 与 fooyin 不一致」，而是「fooyin 弄丢了自己那段
录音的身份」。这需要一条 fooyin 侧的回路。

### 6.2. `snapshot` —— 在改动之前留下证据

`Tracks` 无软删除（§3.2）。文件标签一变，`FilePath → TrackHash → 标签` 的对应关系
当场消失，只剩下一行以「再也推导不出来的哈希」为主键的统计。**哈希无法反推回标签**，
所以事后没有任何东西能修复它。

因此 `snapshot` 不是备份，而是**证据**：一次 `Tracks ⋈ TrackStats` 的读取，记下每行的
精确哈希、归一化哈希、路径、轨号、标题，以及旁路表里的贡献值。它唯一的要求是时序 ——
必须**先于**改动存在。`--path` 可以把范围收窄到一个目录，但默认是全库，正因为收窄的
用法预设了「你事先知道自己要弄坏什么」，而促成本设计的那次事故是几周之后才被发现的。

孤儿无法被快照（没有标签、无从归一化），所以此命令只保护当前健康的部分。

### 6.3. `restore` —— 把统计放回当前身份

```
哈希仍在 Tracks 里？               ──是──▶ 跳过 —— 身份从未断裂
  └─否─▶ 归一化哈希命中？          ──是──▶ 搬移   （标签只改了大小写）
      └─否─▶ FilePath 完全相同？    ──是──▶ 搬移   （原地重打标签）  ⚠
          └─否─▶ 在 --to 范围内按轨号 + 归一化标题？ ──▶ 搬移         ⚠
              └─否─▶ 无法解析
```

前两层是确定性的。`FilePath` 这一层覆盖归一化哈希漏掉的情形 —— 重打标签但**没有**
改名 —— 它与归一化哈希恰成镜像：一个扛得住改名，另一个扛得住重打标签。

最后一层是启发式的，并被双重限制：需要显式的 `--to` 目录，且记录数必须低于
`--fuzzy-limit`（100）。**作用域**才是关键 —— 「track 1 / Intro」在一张专辑内唯一，
放到全库则毫无价值。只限制记录数无法约束**候选集**，只能约束查询集。

### 6.4. `orphans` —— 唯一的报警器

即「哈希不在 `Tracks` 里」的统计行。每一次身份断裂都会落到这里，因此列表非空就意味着
有东西改了身份却没被修复。最初那次事故本该由它发现：16 首曲目、横跨 4 张专辑，已经
失联数周，而没有任何东西说过一句。

默认只读。`--from-json` 会拿每个哈希去快照或导出里查，重新贴回可读的路径。
`PlayCount = 0` 的行默认隐藏（`--all` 可显示）—— 它们既无数据也无信息量，留在默认
输出里只会稀释信号。

**清理。** `restore --prune-moved` 在统计写到别处之后删除源行 —— 完成一次「搬移」而非
留下副本 —— 且仅限确定性匹配；启发式匹配保留其源行，因为「有人扫了一眼 dry-run」不
等于「可以销毁最后一份副本」。`orphans --prune` 是无作用域的全库清扫，它存在的意义是
让报警器保持有效：一个仍然装着所有历史修复记录的列表，就不再是信号了。两者都默认
关闭，且都需要 `--apply`。

全库清扫是本工具风险最高的操作 —— 盘没挂载会让整个库看起来都是孤儿 —— 因此只要有任一
`Libraries.Path` 不在磁盘上，它就拒绝执行。

---

## 7. 安全模型

- 所有写入类命令**默认 dry-run**，必须加 `--apply` 才写入。dry-run 以**只读**打开
  fooyin 并打印变更计划（插入/更新计数、未匹配清单、样例 diff）。
- 仅靠路径尾部命中（`import`）或靠启发式层命中（`restore`）的变更会在计划里标为
  **低置信度**，让那少数猜测在 `--apply` 前得以人工过目。
- `--apply` 在任何写入前把 `fooyin.db` 复制为 `fooyin.db.bak-<时间戳>`。
- 要**撤销**一次导入，恢复它对应的 `fooyin.db.bak-<时间戳>` —— 这会把合并过的
  `TrackStats` 与 `_fb2fooyin_import` 行一并回滚。若改为手动删除旁路表，则会让
  播放数账目失同步、下次导入双计（§3.4）。
- `--apply` 执行 `BEGIN IMMEDIATE`（`importer.begin_write`）；若 fooyin 持有锁则以
  “请先关闭 fooyin”报错中止，而非冒着写坏/写一半的风险。
- 删除永远不是副作用：`--prune-moved` 与 `--prune` 是独立、默认关闭的开关，其中无
  作用域的那个还会在媒体库根目录不在磁盘上时拒绝执行（§6.4）。
- 每份 payload 都标注了生产者（`kind`）；把快照喂给 `import`、或把导出喂给
  `restore`，会直接报错，而不是套用错误的合并规则。
- 所有写入在单个事务内提交。

---

## 8. 边界与非目标

- **不读音频文件。** 标签来自 foobar 的 `metadb.info` 缓存（§2.5）或 fooyin 自己的
  列（§3.2），从不打开音频文件 —— 因此对已转移到网盘的文件也照常工作。
- **复刻哈希，而非音频哈希。** fooyin 的 `TrackHash` 是从标签重算的（§4.1），工具
  从不对音频内容做哈希。
- **不做模糊字段匹配。** 标签列只用于重算哈希（§3.2）。唯一比对文本的启发式层
  （`restore` 的轨号 + 标题）被限定在单个目录内、有数量上限、默认关闭，且永远不被
  允许删除它的源行。
- **`snapshot` 无法追溯过去。** 它保护的是运行那一刻健康的行；发生在首次快照之前的
  断裂只能靠 foobar 修复，且仅限 foobar 仍认识该曲目时（§6.1）。
- **CUE / 子歌曲。** `subsong` 是复刻哈希的一部分（`str(subsong)`），故多子歌曲曲目
  能正确哈希 —— 但本库没有（`Subsong` 恒为 0），因此未经测试。
- **单向**（foobar → fooyin），没有 fooyin → foobar 的反向路径。
- 播放统计 GUID 现**自动识别**（`core.detect_stats_guid`，靠「全 40 字节 `_data`」
  特征），因此换个 foobar 配置无需改代码；`core.STATS_INDEX_GUID` 仅为兜底默认值。
