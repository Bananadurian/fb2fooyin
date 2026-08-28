# fb2fooyin

[English](README.md) · **简体中文**

将 foobar2000 的播放统计迁移到 Linux 上的 [fooyin](https://www.fooyin.org/)。

foobar2000 的 [`foo_playcount`](https://wiki.hydrogenaudio.org/index.php?title=Foobar2000:Components/Playback_Statistics_v3.x_%28foo_playcount%29)
组件把统计存在 `metadb.sqlite` 里，该文件位于 foobar2000 的 **profile** 文件夹：

- **便携版**安装 —— `<安装目录>\profile`，如 `D:\foobar2000\profile`
- **标准版**安装 —— `%APPDATA%\foobar2000\profile`，即
  `C:\Users\<用户>\AppData\Roaming\foobar2000\profile`

fooyin 则存在 `~/.local/share/fooyin/fooyin.db`。本工具迁移其中 5 个字段：

| fooyin 列 | foobar 来源 |
|---|---|
| `PlayCount`   | 播放次数 |
| `FirstPlayed` | 首次播放 |
| `LastPlayed`  | 最近播放 |
| `AddedDate`   | 添加时间 |
| `Rating`      | 星级评分 |

零第三方依赖 —— 仅需 Python 3.10+ 标准库。

**测试环境：** foobar2000 v2.25.x (x64) · `foo_playcount` v3.x · fooyin v0.12.1。

## 如何匹配曲目

fooyin 用**标签的内容哈希**（`TrackHash`）而非路径来标识一段录音 —— 因此文件
移动/改名都不受影响。工具从 foobar 缓存的标签**精确复刻**这个哈希并据此直接匹配。
对 fooyin 只按主艺人归档的多艺人曲目，还会再试**第二个哈希**（同一公式但只取主
艺人），让 collab 按内容而非路径命中。

由于哈希基于**原始**标签字符串，一首被改了大小写的曲目（`A Strange Kind Of Love`
→ `A Strange Kind of Love`，从另一家商店重下专辑时很典型）算出的哈希会完全不同。
此时会再试一个**大小写归一化哈希**把它们救回来。三个哈希合计覆盖 **99.98%** 的
曲目 —— 且因其源于标签，**完全不关心你的目录结构**。

对哈希复刻不了的少量坏元数据残差（如空 tag 文件），回退到**专辑相对路径尾部** ——
即流派目录以下的 `艺人/专辑/文件名` 段，尽管两库的根路径、分隔符、大小写都不同，这
一段完全一致。换到结构不同的库时，这几首只会被报为未匹配；哈希路径不受影响。

## 用法

通过 [uv](https://docs.astral.sh/uv/) 运行 —— 首次运行时它会把这个（零依赖）包
构建进独立环境，无需手动建 venv：

```bash
git clone https://github.com/Bananadurian/fb2fooyin.git
cd fb2fooyin
uv sync                      # 可选：预先创建好环境
uv run fb2fooyin --help      # 查看所有命令与参数

# 1. 导出 foobar 统计到 JSON（只读）。先把你 foobar2000 profile 的 metadb.sqlite
#    放到 ./data/metadb.sqlite，或用 --foobar-db PATH 指定。输出 ./stats.json。
uv run fb2fooyin export

# 2. 预览导入（dry-run，不写入）。请先关闭 fooyin。默认读取 ./stats.json 与
#    ~/.local/share/fooyin/fooyin.db。
uv run fb2fooyin import

# 3. 真正写入（先自动备份 fooyin.db；若 fooyin 正在运行则拒绝）。
uv run fb2fooyin import --apply
```

任一默认值都可用 `--foobar-db` / `--out` / `--json` / `--fooyin-db` 覆盖。没有 uv？
本包纯标准库，用 `python3 -m fb2fooyin …` 作为回退。

## 让播放数扛住文件改动（`snapshot` / `restore`）

foobar 一旦停用就帮不上忙了：你在 fooyin 里新增的播放、以及 foobar 从未见过的
专辑，都只存在于 fooyin 一处。而当你重打标签或替换文件时，fooyin 会生成新的
`TrackHash`，旧的统计行随即失联 —— 它的标签随旧行一起被删除，而**哈希无法反推回
标签**，所以事后没有任何办法修复。

因此要事先留证：

```bash
# 改文件之前先拍快照。一次读取，不写入。
uv run fb2fooyin snapshot --out snapshot.json

# …重打标签 / 改名 / 替换文件，让 fooyin 重扫…

# 把统计放回这段录音现在的身份上（先 dry-run）。
uv run fb2fooyin restore --json snapshot.json
uv run fb2fooyin restore --json snapshot.json --apply
```

`restore` 会跳过身份未变的记录，然后依次用归一化哈希（标签只改了大小写）、完整
文件路径精确相等（原地重打标签）来匹配。两者都失败时，可以指定目标目录来启用
最后的「轨号 + 标题」配对 —— 这类猜测只有限定在单张专辑内才安全，所以它既限定
作用域也限定规模：

```bash
uv run fb2fooyin restore --json snapshot.json --to "/path/to/the/new/album"
```

加 `--prune-moved` 可在统计写到新位置后删除源行（仅限确定性匹配；靠猜测配上的会
保留源行）。

### 知道出事了（`orphans`）

「统计行还在、对应曲目已消失」是你唯一能拿到的信号。隔段时间看一眼 —— 列表非空
就意味着有东西改了身份却没修：

```bash
uv run fb2fooyin orphans --from-json snapshot.json    # 只读
uv run fb2fooyin orphans --prune                      # 清理的 dry-run
uv run fb2fooyin orphans --prune --apply              # 真正删除
```

`--from-json` 会给每条孤儿还原出可读的路径。当任一媒体库根目录不在磁盘上时，清理
会直接拒绝执行 —— 盘没挂载会让整个库看起来都成了孤儿。

### 自动化快照

这套方案依赖快照**先于**改动存在。如果不想靠记性，一个每周执行的 systemd user
timer 能把最坏损失压到一周。新建
`~/.config/systemd/user/fb2fooyin-snapshot.service`：

```ini
[Unit]
Description=Snapshot fooyin playback stats

[Service]
Type=oneshot
WorkingDirectory=%h/path/to/fb2fooyin
ExecStart=/usr/bin/uv run fb2fooyin snapshot --out %h/.local/share/fb2fooyin/snapshot-%%Y%%m%%d.json
```

以及 `~/.config/systemd/user/fb2fooyin-snapshot.timer`：

```ini
[Unit]
Description=Weekly fooyin stats snapshot

[Timer]
OnCalendar=weekly
Persistent=true

[Install]
WantedBy=timers.target
```

然后 `systemctl --user enable --now fb2fooyin-snapshot.timer`。

## 人工核对单曲（`inspect`）

想手动确认某首歌在两个播放器里是否一致，用 `inspect` 并排打印 foobar 与
fooyin 的数据（只读，不写入）。查询是专辑相对路径的忽略大小写子串：

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

`merged` 是一次导入会写入的结果 —— 与导入同一套合并规则、且感知 sidecar。此处它
等于 `fooyin`，因为该曲已导入过（重跑是安全的空操作）；对一首全新曲目，它会显示
foobar 的播放数叠加到 fooyin 上、first/added 取最早、last 取最晚。`hash` 行是
foobar 重算 vs fooyin 存储的 `TrackHash`，`match` 说明该曲是按哈希匹配、还是回退
到了尾部。

导入前（预看会变什么）与导入后（确认已写入）都好用。

## 合并规则

**导入（foobar → fooyin）—— 相加**，因为两个播放器各自独立累积播放：

- **PlayCount** —— 相加但幂等。旁路表 `_fb2fooyin_import` 记录本工具每次贡献了
  多少，因此重跑（或 foobar 计数增长）都能正确落地，且期间你在 fooyin 里新增的
  播放不会丢。
- **FirstPlayed / AddedDate** —— 取更早的值。
- **LastPlayed** —— 取更晚的值。
- **Rating** —— foobar 已评分时以 foobar 为准，否则保留 fooyin 的。加
  `import --keep-fooyin-rating` 则绝不覆盖 fooyin 里已有的评分（foobar 仍会补空缺的）。
- 时间戳为 0 表示“从未”，不会覆盖真实值。
- 同一个文件在 foobar 里的重复条目（媒体库根目录改过名，旧路径写法还留着）会在
  合并前先折叠 —— 否则每首歌的播放数会被乘上残留根目录的份数。

**还原（fooyin → 自己的快照）—— 取 max，不相加**。把快照还原到一个没有任何改动
的数据库上必须是空操作，所以播放数取两者较大值而非求和。sidecar 行会随统计一起
迁移，这样以后再跑 foobar 导入时不会把它的贡献再算一遍。

## 安全性

- 所有写入类命令都**默认 dry-run**，必须加 `--apply` 才写入。
- 仅靠路径尾部命中（`import`）或靠轨号+标题配上（`restore`）的变更会在 dry-run 里
  标为**低置信度**，方便你在 `--apply` 前过目那少数几条。
- `--apply` 会先把 `fooyin.db` 备份为 `fooyin.db.bak-<时间戳>`。
- 要重做或撤销一次导入，请恢复某个 `fooyin.db.bak-<时间戳>` —— 不要手动删除
  `_fb2fooyin_import` 旁路表，那会让播放数账目失同步、下次导入双计（见
  [DESIGN.zh-CN.md](DESIGN.zh-CN.md) §3.4）。
- 若 fooyin 持有数据库锁则拒绝写入（请先关闭 fooyin）。
- 删除行永远不是副作用：`--prune-moved` / `--prune` 是独立且默认关闭的开关，其中
  全库清理还会在任一媒体库根目录不在磁盘上时拒绝执行。
- 每份 JSON 都记录了它由哪个命令产出，把快照喂给 `import`（或把导出喂给
  `restore`）会直接报错，而不是套用错误的合并规则。
- 所有写入在单个事务内完成。

## 编码（均对真实库核对过）

- 时间戳：Windows FILETIME（自 1601 起的 100 纳秒计数）→ Unix **毫秒**。
- 评分字节 → 星级：`0x3F`=1、`0x6A`=2、`0x95`=3、`0xBF`=4、`0xEA`=5、
  `0xFF`=未评分。fooyin 以 `星/5` 存为归一化 REAL（`-1.0` 表示未评分）——在 fooyin
  里选 1-5 / 1-10 / 1-100 刻度只改显示、不改存储。

## 测试

```bash
uv run --with pytest pytest      # 临时环境拉取 pytest 运行
```

更多设计细节见 [DESIGN.zh-CN.md](DESIGN.zh-CN.md)。

## 许可证

[MIT](LICENSE) © Bananadurian
