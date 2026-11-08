# 词库构建

插件本身不含词库。词库是**可重建产物**，需要自己下一次 90MB 的数据并建库，全程约 3 分钟（下载另计 5–30 分钟，取决于网络）。

## 数据源

| 项 | 说明 |
|---|---|
| 地址 | https://kaikki.org/dictionary/Russian/kaikki.org-dictionary-Russian.jsonl.gz |
| 本质 | Wiktionary 俄语词条的机器可读抽取（wiktextract） |
| 体积 | 压缩 90,794,686 字节 / 解压约 948MB |
| 内容 | 原形、全部屈折形式（名词六格 × 单复数、动词变位与形动词、形容词长尾短尾）、重音、IPA、发音音频链接、英文释义、例句 |
| 许可 | CC BY-SA 4.0，可商用，需署名「Wiktionary / Kaikki.org」 |

> 顺带说明：OpenRussian 已经没有整库 dump 下载入口，只剩 TogetherDB 的在线导出，所以走 Kaikki。Kaikki 的屈折表正好覆盖 OpenRussian 最值钱的那部分。

## 建库

**Linux / macOS**

```bash
./scripts/download_data.sh
python3 scripts/build_db.py --input data/ru.jsonl.gz --output data/ru_dict.db
```

**Windows（PowerShell）**

```powershell
.\scripts\download_data.ps1
python .\scripts\build_db.py --input data\ru.jsonl.gz --output data\ru_dict.db
```

Windows 10 以上自带 `curl.exe`，脚本直接调用它续传；也可以用浏览器手动下载那个 URL 放到 `data\ru.jsonl.gz`。

建库输出示例：

```
完成：原始 443260 行，有效 443168 行，合并为 438894 个词条，1584297 条词形，耗时 135.5s -> data/ru_dict.db
```

词库大小约 307MB。

## 装到 AstrBot

把 `data/ru_dict.db` 复制到 AstrBot 的 `data/` 目录，插件默认配置的 `db_path` 就是 `data/ru_dict.db`。如果放到别处，在 WebUI 的插件配置里改 `db_path`。

## 建库做了什么

三张表：

- `entry` 词条：带重音原形、归一化键、词性、IPA、音频、体、体对、英文释义、例句
- `form` 屈折形式反查表（`книгами → книга`、`шёл → идти`）
- `zh_cache` 中文释义缓存（大模型译过一次就不再调用）

归一化规则：小写 + `ё→е` + 去组合重音符号 `U+0301`/`U+0300` + 去标点。用户不打重音、不打 ё 都能查到。

同形异重音词用**带重音的原形**做合并键，所以 `за́мок`（城堡）和 `замо́к`（锁）不会被错误合并成一条。

## 验证

不装 AstrBot 也能直接测查询：

```bash
python3 -c "
import sys; sys.path.insert(0, '.')
from dict_core import RuDict, render
d = RuDict('data/ru_dict.db')
print(render(d.lookup('книгами'), core=d))
print(render(d.lookup('замок'), core=d))
"
```

## 常见问题

| 现象 | 原因与处理 |
|---|---|
| 下载中断 | `download_data.sh` / `.ps1` 都带 `-C -` 断点续传，重跑即可 |
| 建库报内存不足 | 逐行流式解析，正常占用不到 1GB；确认是解压后的 948MB 在磁盘上而不是内存里 |
| 插件提示词库缺失 | `db_path` 指错了，或 `ru_dict.db` 还没生成 |
| 某些变形查不到 | Wiktionary 未收录该形式，可装 `pymorphy3` 做兜底还原 |
