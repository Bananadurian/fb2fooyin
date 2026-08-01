# fb2fooyin

[English](README.md) · **简体中文**

将 foobar2000 的播放统计迁移到 Linux 上的 [fooyin](https://www.fooyin.org/)。

foobar2000 的 `foo_playcount` 组件把统计存在 `metadb.sqlite` 里，fooyin 则存在
`~/.local/share/fooyin/fooyin.db`。本工具迁移其中 5 个字段：

| fooyin 列 | foobar 来源 |
|---|---|
| `PlayCount`   | 播放次数 |
| `FirstPlayed` | 首次播放 |
| `LastPlayed`  | 最近播放 |
| `AddedDate`   | 添加时间 |
| `Rating`      | 星级评分 |

零第三方依赖 —— 仅需 Python 3.10+ 标准库。

## 如何匹配曲目

两个库的根路径、路径分隔符、流派目录大小写都不同
（`D:\11_MusicLib\11.11_C-Pop\…` 对 `/home/xre/11_music/11.11_c-pop/…`），
但流派目录**以下**的相对部分（`艺人/专辑/文件名`）完全一致。工具即以这段忽略
大小写的“路径尾部”作为匹配键（实测命中率 99.9%）。fooyin 的统计是按内容型
`TrackHash` 存储的，因此导入时按 `尾部 → Tracks.FilePath → TrackHash →
TrackStats` 链式解析。

## 用法

```bash
cd _tool/fb2fooyin

# 1. 导出 foobar 统计到 JSON（默认读取 data/metadb.sqlite，只读）
python3 -m fb2fooyin export --out stats.json

# 2. 预览导入（dry-run，不写入任何内容）。请先关闭 fooyin。
python3 -m fb2fooyin import --json stats.json

# 3. 真正写入（先自动备份 fooyin.db；若 fooyin 正在运行则拒绝）
python3 -m fb2fooyin import --json stats.json --apply
```

默认路径已指向本库，直接 `export` / `import` 亦可。

## 合并规则（导入）

对 fooyin 已有行采取合并而非直接覆盖：

- **PlayCount** —— 相加但幂等。旁路表 `_fb2fooyin_import` 记录本工具每次贡献了
  多少，因此重跑（或 foobar 计数增长）都能正确落地，且期间你在 fooyin 里新增的
  播放不会丢。
- **FirstPlayed / AddedDate** —— 取更早的值。
- **LastPlayed** —— 取更晚的值。
- **Rating** —— foobar 已评分时以 foobar 为准，否则保留 fooyin 的。
- 时间戳为 0 表示“从未”，不会覆盖真实值。

## 安全性

- 导入**默认 dry-run**，必须加 `--apply` 才写入。
- `--apply` 会先把 `fooyin.db` 备份为 `fooyin.db.bak-<时间戳>`。
- 若 fooyin 持有数据库锁则拒绝写入（请先关闭 fooyin）。
- 所有写入在单个事务内完成。

## 编码（均对真实库核对过）

- 时间戳：Windows FILETIME（自 1601 起的 100 纳秒计数）→ Unix **毫秒**。
- 评分字节 → 星级：`0x3F`=1、`0x6A`=2、`0x95`=3、`0xBF`=4、`0xEA`=5、
  `0xFF`=未评分。fooyin 以 `星/5` 存为 REAL（`-1.0` 表示未评分）。

## 测试

```bash
python3 -m pytest tests/    # 若未装 pytest，可用标准库简易 runner
```

更多设计细节见 [DESIGN.zh-CN.md](DESIGN.zh-CN.md)。
