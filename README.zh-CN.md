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
移动/改名都不受影响。工具从 foobar 缓存的标签**精确复刻**这个哈希并据此直接匹配
（约 99% 的曲目，还能救回路径已变更的曲目）。对少数无法复刻的残差（如多艺人
m4a，两个播放器读取艺人列表的方式不同），回退到**专辑相对路径尾部** —— 即流派
目录以下的 `艺人/专辑/文件名` 段，尽管两库的根路径、分隔符、大小写都不同，这一段
完全一致。对两库都存在的曲目，合并覆盖率 100%。

## 用法

通过 [uv](https://docs.astral.sh/uv/) 运行 —— 首次运行时它会把这个（零依赖）包
构建进独立环境，无需手动建 venv：

```bash
cd _tool/fb2fooyin
uv sync                      # 可选：预先创建好环境

# 1. 导出 foobar 统计到 JSON（默认读取 data/metadb.sqlite，只读）
uv run fb2fooyin export --out stats.json

# 2. 预览导入（dry-run，不写入任何内容）。请先关闭 fooyin。
uv run fb2fooyin import --json stats.json

# 3. 真正写入（先自动备份 fooyin.db；若 fooyin 正在运行则拒绝）
uv run fb2fooyin import --json stats.json --apply
```

默认路径已指向本库，直接 `export` / `import` 亦可。没有 uv？本包纯标准库，可在此
目录用 `python3 -m fb2fooyin …` 作为回退。

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

## 合并规则（导入）

对 fooyin 已有行采取合并而非直接覆盖：

- **PlayCount** —— 相加但幂等。旁路表 `_fb2fooyin_import` 记录本工具每次贡献了
  多少，因此重跑（或 foobar 计数增长）都能正确落地，且期间你在 fooyin 里新增的
  播放不会丢。
- **FirstPlayed / AddedDate** —— 取更早的值。
- **LastPlayed** —— 取更晚的值。
- **Rating** —— foobar 已评分时以 foobar 为准，否则保留 fooyin 的。加
  `import --keep-fooyin-rating` 则绝不覆盖 fooyin 里已有的评分（foobar 仍会补空缺的）。
- 时间戳为 0 表示“从未”，不会覆盖真实值。

## 安全性

- 导入**默认 dry-run**，必须加 `--apply` 才写入。
- `--apply` 会先把 `fooyin.db` 备份为 `fooyin.db.bak-<时间戳>`。
- 要重做或撤销一次导入，请恢复某个 `fooyin.db.bak-<时间戳>` —— 不要手动删除
  `_fb2fooyin_import` 旁路表，那会让播放数账目失同步、下次导入双计（见
  [DESIGN.zh-CN.md](DESIGN.zh-CN.md) §3.4）。
- 若 fooyin 持有数据库锁则拒绝写入（请先关闭 fooyin）。
- 所有写入在单个事务内完成。

## 编码（均对真实库核对过）

- 时间戳：Windows FILETIME（自 1601 起的 100 纳秒计数）→ Unix **毫秒**。
- 评分字节 → 星级：`0x3F`=1、`0x6A`=2、`0x95`=3、`0xBF`=4、`0xEA`=5、
  `0xFF`=未评分。fooyin 以 `星/5` 存为 REAL（`-1.0` 表示未评分）。

## 测试

```bash
uv run --with pytest pytest      # 临时环境拉取 pytest 运行
```

更多设计细节见 [DESIGN.zh-CN.md](DESIGN.zh-CN.md)。

## 许可证

[MIT](LICENSE) © Bananadurian
