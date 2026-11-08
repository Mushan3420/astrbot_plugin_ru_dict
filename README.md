# astrbot_plugin_ru_dict · 俄语查词助手

AstrBot 插件，给 QQ 官方机器人（也兼容 aiocqhttp / Telegram / 飞书）加上俄语单词与短语查询能力。

词库来自 Wiktionary 的机器可读抽取（Kaikki.org），43.9 万词条、158 万条词形，本地 SQLite 检索，毫秒级响应，零第三方依赖。

## 功能

| 能力 | 说明 |
|---|---|
| 查词 | 词义、词性、重音与 IPA、体（完成体/未完成体）、体对、例句 |
| 屈折反查 | 直接发变形式也能查到原形，并标注这是第几格第几人称：`книгами → книга`、`шёл → идти`、`люди → человек` |
| 变形表 | 名词：性 + 单数六格 + 复数六格；动词：现在时六人称 + 过去时四性 + 命令式 + 形动词 + 副动词；形容词：长尾 + 短尾 + 比较级 + 最高级 |
| 中文释义 | 可选调用当前大模型提供商把英文释义译成中文，写入本地缓存，同一个词只调一次 |
| 拼写建议 | 查不到时按编辑距离给候选，只在「前两字母相同」的池子里比，避免全表扫描 |
| 大模型工具 | 注册 `ru_lookup` 函数工具，用户问「книга 的复数怎么变」时由模型自行调用，返回结构化事实而不是自由生成 |

## 安装

插件本体不含词库（307MB，超市场包体积上限），装好后要拿到词库，两条路：

- **推荐**：WebUI 里点「下载词库」按钮，从 GitHub Release 一键拉取（见下一节）
- **手动**：自己跑 [scripts/README.md](scripts/README.md) 里的建库脚本，全程约 3 分钟（下载另计）

三种安装方式任选：

1. **插件市场**：AstrBot WebUI → 插件 → 插件市场，搜索「俄语查词助手」
2. **从仓库安装**：插件 → 安装（+）→ 从链接安装，填本仓库地址
3. **从文件安装**：下载 Release 里的 `astrbot_plugin_ru_dict.zip`，插件 → 安装（+）→ 从文件安装

装好后把词库放到 `AstrBot/data/ru_dict.db`（或改配置里的 `db_path` 指到别处），在 WebUI 点「重载插件」。

## 一键下载词库

插件自带一个 WebUI 页面：**插件 → 俄语查词助手 → 俄语词库**（AstrBot 扫描到 `pages/dict/index.html` 后出现在插件详情页）。页面显示当前词库的路径、大小、词条数和是否已加载，下面一个「下载词库」按钮。

点击后的流程：

1. 流式下载 `db_url` 指向的 `ru_dict.db.gz`（默认走 `releases/latest/download/`，跟着 GitHub 的 302 到 CDN）
2. 解压到 `<db_path>.download` 临时文件
3. 打开临时库数一遍 `entry` 表，少于 10 万条就判定为坏包/半截包，报错并清理
4. 校验通过才 `os.replace` 覆盖正式库，然后重开 SQLite 连接（不用重载插件，下一次查词就是新库）
5. 页面每秒轮询进度，显示已下载 MB 与速度

页面上能看到失败原因（HTTP 状态码、解压错误、词条数不足），不会是静默失败。

### 仓库作者要做的准备

Release 里要有一个名为 `ru_dict.db.gz` 的附件。打包前先让库变成自包含的单文件（WAL 里可能还有没回写的数据，直接 gzip 会丢）：

```bash
python3 -c "import sqlite3;c=sqlite3.connect('ru_dict.db');c.execute('PRAGMA wal_checkpoint(TRUNCATE)');c.execute('PRAGMA journal_mode=DELETE');c.close()"
gzip -9 ru_dict.db          # 得到 ru_dict.db.gz，约 120-130MB
```

然后在 GitHub 上建 Release（tag 例如 `v1.0.0`），把 `ru_dict.db.gz` 传上去发布即可。这样下载地址就是稳定的：

```
https://github.com/<你的用户名>/astrbot_plugin_ru_dict/releases/latest/download/ru_dict.db.gz
```

仓库地址已经填好（`Mushan3420/astrbot_plugin_ru_dict`），插件默认就走这个 Release 地址，一般不需要改。要换成别处的源，改插件配置里的 `db_url` 即可。

## 指令

```
/ru книга          查单词
/ru книгами        直接发变形也能查，会提示「工具、复数」
/ru до свидания    查短语
/变形 идти         完整变位表
/变形 красивый     完整变格表
/ru                使用说明
```

群聊里需要 @ 机器人才会触发（QQ 官方平台限制，群消息只有 `GROUP_AT_MESSAGE_CREATE` 这一种）。

## 配置

| 配置项 | 默认 | 说明 |
|---|---|---|
| `db_path` | `data/ru_dict.db` | 词库路径，相对路径按 AstrBot 运行目录解析 |
| `db_url` | 空 | Release 里 `ru_dict.db.gz` 的直链；留空则用代码里的默认地址 |
| `render_mode` | `text` | `text` 纯文本；`image` 走 AstrBot 文转图出词卡（需要 Playwright） |
| `use_llm` | `true` | 是否调用当前 LLM 提供商把英文释义译成中文 |

## 大模型工具

插件通过 `@filter.llm_tool(name="ru_lookup")` 注册了函数工具，两条路径并存：

```
关键词指令 /ru книга   → 直出词卡，不经过 LLM（快、零 token、格式稳定）
自然语言提问           → LLM 调 ru_lookup → 基于返回数据组织回答
```

工具返回的是给模型看的结构化文本，末尾带一句「请只使用以上信息作答，不要补充未给出的释义或变形」，用来抑制编造变格表。

`llm_tool` 是全局注册，所有走该 LLM 提供商的会话都能看到这个工具。如果机器人是多用途的，建议在人格（Persona）的 `tools` 字段里限定哪些人格可用。

## 与知识库的分工

| 问题类型 | 走哪里 |
|---|---|
| 「книга 什么意思 / 复数怎么变 / шёл 是哪来的」 | 本插件（精确、毫秒级、有据可查） |
| 「完成体和未完成体怎么选」「俄语格位体系讲一下」 | 知识库（成段讲解，语义召回合适） |

不要把词条本身也灌进知识库：屈折反查、六格表格、体对这几件事 RAG 做不了，而且会和 SQLite 里的释义形成两套口径。中文释义统一走插件的 `zh_cache`。

## 目录结构

```
astrbot_plugin_ru_dict/
├── main.py              指令、llm_tool、中文译义
├── dict_core.py         查询核心：归一化、屈折反查、变形表渲染、缓存
├── metadata.yaml        插件元信息
├── _conf_schema.json    WebUI 配置项
├── pages/dict/index.html  WebUI 里的「俄语词库」页面（下载按钮）
├── card.html            图片词卡模板（render_mode=image）
├── requirements.txt     依赖声明（无第三方依赖）
├── README.md
└── scripts/             词库构建脚本（不参与插件加载）
    ├── build_db.py
    ├── download_data.sh
    ├── download_data.ps1
    └── README.md
```

## QQ 官方平台的几条约束

被动回复窗口是群聊 5 分钟、每条用户消息最多回 5 次，所以插件一次查询只发 1 条消息，超长内容截断而不是拆条。单群 20 qpm 频控下，本地 SQLite 读取足够用；LLM 译义命中缓存后不再请求。markdown 和消息按钮要单独申请模板能力，所以默认走纯文本，输出里也不拼外链（官方要求消息正文含 URL 需先在后白名单登记）。

## 已知限制

- 中文释义依赖大模型，是模型输出，专业术语建议人工校对后再固化
- 短语覆盖有限，Wiktionary 收录的固定搭配不全，查不到时给拼写建议
- 生僻屈折形式查不到，可装 `pymorphy3` 在查询失败分支做兜底还原
- 同形异重音词（`за́мок` 城堡 / `замо́к` 锁）按带重音的原形建键，两条都会返回

## 许可与署名

词库派生自 Wiktionary，经 Kaikki.org 抽取，许可 **CC BY-SA 4.0**。分发或再发布时请保留署名「Wiktionary / Kaikki.org」，插件输出里的词卡页脚已包含该署名。
