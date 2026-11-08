#!/usr/bin/env python3
"""
AstrBot 俄语助手插件（QQ 官方机器人适配）

指令：
    /ru <单词或短语>     查词
    /变形 <单词>         完整变格/变位表
    /ru                  使用说明
"""

import asyncio
import gzip
import hashlib
import os
import re
import shutil
import sqlite3
import time

try:
    from quart import jsonify
except ImportError:  # 理论上 AstrBot 自带 quart，兜底避免插件加载失败

    def jsonify(obj):
        return obj


from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:  # 作为包加载（AstrBot 常规方式）
    from .dict_core import POS_ZH, RuDict, render, tags_zh
except ImportError:  # 兜底：按顶级模块加载时
    from dict_core import POS_ZH, RuDict, render, tags_zh

CMD_RU = ("ru", "俄语", "查俄语")
CMD_FORMS = ("变形", "变格", "变位")

PLUGIN_NAME = "astrbot_plugin_ru_dict"
DB_ASSET = "ru_dict.db.gz"
DEFAULT_DB_URL = (
    f"https://github.com/Mushan3420/{PLUGIN_NAME}/releases/latest/download/{DB_ASSET}"
)
MIN_ENTRIES = 100000  # 下载后至少要这么多词条才认，防止下到半截或下错文件

LLM_SYSTEM = (
    "你是俄汉词典编辑。把给定的俄语词典释义逐条译成简明、地道的汉语。"
    "只输出译文，每条一行，行数必须与输入条数完全一致；"
    "不要编号、不要解释、不要加引号、不要补充原文没有的含义。"
    "遇到「X of Y」这类语法说明，译成「……的……形式」。"
)


@register(
    "astrbot_plugin_ru_dict",
    "Mushan3420",
    "俄语查词助手",
    "1.0.0",
    "https://github.com/Mushan3420/astrbot_plugin_ru_dict",
)
class RuDictPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.core: RuDict | None = None
        self.db_path = self._resolve_db(config.get("db_path", "data/ru_dict.db"))
        if self.db_path and os.path.exists(self.db_path):
            try:
                self.core = RuDict(self.db_path)
                logger.info(f"[ru_dict] 已加载词库 {self.db_path}")
            except Exception as e:  # noqa: BLE001 —— 插件不能因单次异常崩溃
                logger.error(f"[ru_dict] 词库加载失败: {e}")

        self._entries_cache: tuple | None = None
        self._dl_task: asyncio.Task | None = None
        self._dl: dict = {
            "state": "idle",
            "bytes": 0,
            "total": 0,
            "message": "",
            "speed": 0.0,
            "t0": 0.0,
        }
        self._register_pages(context)

    # ---------- 工具 ----------

    # ---------- 插件页面（WebUI 里的一键下载） ----------

    def _register_pages(self, context: Context) -> None:
        """注册插件页面的后端接口；低版本 AstrBot 没有这个方法就跳过。"""
        register = getattr(context, "register_web_api", None)
        if register is None:
            logger.warning(
                "[ru_dict] 当前 AstrBot 不支持插件页面，词库请用 scripts/ 里的脚本手动构建"
            )
            return
        try:
            register(f"/{PLUGIN_NAME}/status", self.page_status, ["GET"], "词库状态")
            register(
                f"/{PLUGIN_NAME}/download", self.page_download, ["POST"], "下载词库"
            )
            register(
                f"/{PLUGIN_NAME}/progress", self.page_progress, ["GET"], "下载进度"
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"[ru_dict] 注册页面接口失败: {e}")

    def _db_url(self) -> str:
        url = str(self.config.get("db_url") or DEFAULT_DB_URL).strip()
        if "your-name" in url:
            return ""
        return url

    def _db_info(self) -> dict:
        info: dict = {"path": self.db_path, "exists": os.path.exists(self.db_path)}
        if info["exists"]:
            try:
                st = os.stat(self.db_path)
                info["size"] = st.st_size
                info["mtime"] = time.strftime(
                    "%Y-%m-%d %H:%M", time.localtime(st.st_mtime)
                )
                info["entries"] = self._entries(self.db_path, st.st_mtime, st.st_size)
            except Exception as e:  # noqa: BLE001
                info["error"] = str(e)
        info["loaded"] = self.core is not None
        return info

    def _entries(self, path: str, mtime: float, size: int) -> int:
        """词条数按 mtime+size 缓存，避免页面每次刷新都全表 count。"""
        key = (path, mtime, size)
        if self._entries_cache and self._entries_cache[0] == key:
            return self._entries_cache[1]
        n = self._count_entries(path)
        self._entries_cache = (key, n)
        return n

    @staticmethod
    def _count_entries(path: str) -> int:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return int(conn.execute("SELECT count(*) FROM entry").fetchone()[0])
        finally:
            conn.close()

    async def page_status(self):
        return jsonify(
            {
                "ok": True,
                "db": self._db_info(),
                "download": dict(self._dl),
                "url": self._db_url(),
            }
        )

    async def page_progress(self):
        d = dict(self._dl)
        if d["state"] == "running" and d["total"]:
            d["percent"] = round(d["bytes"] * 100 / d["total"], 1)
        return jsonify({"ok": True, "download": d})

    async def page_download(self):
        if self._dl_task and not self._dl_task.done():
            return jsonify({"ok": False, "message": "已经在下载了，请稍等"})
        url = self._db_url()
        if not url:
            return jsonify(
                {
                    "ok": False,
                    "message": "没有配置下载地址，请在插件配置里把 db_url 填成你的 Release 地址",
                }
            )
        self._dl = {
            "state": "running",
            "bytes": 0,
            "total": 0,
            "message": "开始下载…",
            "speed": 0.0,
            "t0": time.time(),
        }
        self._dl_task = asyncio.create_task(self._download_db(url))
        return jsonify({"ok": True, "message": "已开始下载"})

    async def _download_db(self, url: str) -> None:
        """下载 .db.gz → 解压到临时文件 → 校验词条数 → 原子替换到 db_path。"""
        gz = self.db_path + ".download.gz"
        tmp = self.db_path + ".download"
        try:
            import aiohttp

            os.makedirs(
                os.path.dirname(os.path.abspath(self.db_path)) or ".", exist_ok=True
            )
            timeout = aiohttp.ClientTimeout(total=None, connect=20, sock_read=120)
            async with (
                aiohttp.ClientSession(timeout=timeout) as sess,
                sess.get(url, allow_redirects=True) as resp,
            ):
                if resp.status != 200:
                    raise RuntimeError(
                        f"下载失败，HTTP {resp.status}。请确认 Release 里有 {DB_ASSET}"
                    )
                self._dl["total"] = int(resp.headers.get("Content-Length") or 0)
                done = 0
                # 本地磁盘写 256KB 块，耗时远小于一次网络 await，保持同步写法更简单
                with open(gz, "wb") as f:  # noqa: ASYNC230
                    async for chunk in resp.content.iter_chunked(262144):
                        f.write(chunk)
                        done += len(chunk)
                        elapsed = time.time() - self._dl["t0"]
                        self._dl["bytes"] = done
                        self._dl["speed"] = done / elapsed if elapsed > 0 else 0
                        self._dl["message"] = f"已下载 {done / 1048576:.1f} MB"

            self._dl["message"] = "解压中…"
            with gzip.open(gz, "rb") as src, open(tmp, "wb") as dst:  # noqa: ASYNC230
                shutil.copyfileobj(src, dst, 1048576)

            self._dl["message"] = "校验中…"
            n = self._count_entries(tmp)
            if n < MIN_ENTRIES:
                raise RuntimeError(f"词库不完整，只读到 {n} 个词条")

            os.replace(tmp, self.db_path)
            self._entries_cache = None
            self._reload_core()
            self._dl.update(state="done", message=f"完成，共 {n} 个词条")
            logger.info(f"[ru_dict] 词库下载完成，{n} 个词条 -> {self.db_path}")
        except Exception as e:  # noqa: BLE001 —— 下载失败要让用户看到原因，不能静默
            logger.error(f"[ru_dict] 词库下载失败: {e}")
            self._dl.update(state="error", message=str(e))
        finally:
            for f in (gz, tmp, tmp + "-wal", tmp + "-shm", gz + "-wal", gz + "-shm"):
                try:
                    os.remove(f)
                except OSError:
                    pass

    def _close_core(self) -> None:
        if self.core is not None:
            try:
                self.core._conn.close()
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[ru_dict] 关闭词库连接出错: {e}")
            self.core = None

    def _reload_core(self) -> None:
        self._close_core()
        try:
            self.core = RuDict(self.db_path)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[ru_dict] 新词库加载失败: {e}")

    def _resolve_db(self, p: str) -> str:
        p = os.path.expanduser(str(p))
        if os.path.isabs(p):
            return p
        # 相对路径按 AstrBot 运行目录解析，同时兼容插件目录下的相对路径
        cand = [
            os.path.join(os.getcwd(), p),
            os.path.join(os.path.dirname(__file__), p),
        ]
        for c in cand:
            if os.path.exists(c):
                return c
        return cand[0]

    def _strip_cmd(self, event: AstrMessageEvent, cmds) -> str:
        txt = (event.message_str or "").strip().lstrip("/").strip()
        for c in cmds:
            if txt.startswith(c):
                return txt[len(c) :].strip(" 　:：")
        return txt

    def _missing_db_msg(self) -> str:
        return (
            "词库还没装好。打开 AstrBot WebUI → 插件 → 俄语查词助手 →「俄语词库」页面，"
            "点「下载词库」按钮即可一键下载并安装；也可以在本机跑 scripts/ 里的脚本自己构建。"
        )

    async def _translate(
        self, event: AstrMessageEvent, entries
    ) -> dict[int, list[str]]:
        """把词条英文释义译成中文，按 entry id 返回；失败返回空 dict。"""
        if not self.config.get("use_llm", True) or not entries:
            return {}
        out: dict[int, list[str]] = {}
        pending = []
        for e in entries[:2]:
            glosses = e["glosses"][:3]
            key = (
                f"{e['id']}|{hashlib.md5('||'.join(glosses).encode()).hexdigest()[:10]}"
            )
            cached = self.core.get_zh(key)
            if cached:
                out[e["id"]] = cached
            else:
                pending.append((e, key, glosses))
        if not pending:
            return out

        try:
            prov = await self.context.get_using_provider_async(
                umo=event.unified_msg_origin
            )
            if not prov:
                return out
            total = sum(len(p[2]) for p in pending)
            # 不加编号：让模型按原顺序逐行输出，便于按行数对回义项
            prompt = "\n".join(g for (_, _, gs) in pending for g in gs)
            resp = await asyncio.wait_for(
                prov.text_chat(prompt=prompt, system_prompt=LLM_SYSTEM), timeout=10
            )
            text = (resp.completion_text or "").strip()
            lines = [
                re.sub(r"^\s*[\d、\.\-*]+\s*", "", ln).strip()
                for ln in text.splitlines()
                if ln.strip()
            ]
            if len(lines) != total:
                logger.warning(
                    f"[ru_dict] 译义行数不匹配：期望 {total}，得到 {len(lines)}"
                )
                return out
            i = 0
            for e, key, gs in pending:
                seg = lines[i : i + len(gs)]
                i += len(gs)
                if seg:
                    out[e["id"]] = seg
                    self.core.put_zh(key, seg)
        except Exception as e:  # noqa: BLE001 —— 插件不能因单次异常崩溃
            logger.warning(f"[ru_dict] LLM 译义失败，回退英文释义: {e}")
        return out

    # ---------- 指令 ----------

    @filter.command("ru")
    async def cmd_ru(self, event: AstrMessageEvent):
        """查俄语单词或短语，例：/ru книга"""
        async for r in self._handle_ru(event):
            yield r

    @filter.command("俄语")
    async def cmd_ru_zh(self, event: AstrMessageEvent):
        """查俄语单词或短语（同 /ru）"""
        async for r in self._handle_ru(event):
            yield r

    @filter.command("变形")
    async def cmd_forms(self, event: AstrMessageEvent):
        """查看完整变格/变位表，例：/变形 книга"""
        if not self.core:
            yield event.plain_result(self._missing_db_msg())
            return
        word = self._strip_cmd(event, CMD_FORMS)
        if not word:
            yield event.plain_result("用法：/变形 книга")
            return
        try:
            res = self.core.lookup(word)
            if res["kind"] in ("none", "empty"):
                yield event.plain_result(render(res, core=self.core))
                return
            blocks = []
            for e in res["entries"][:1]:
                lines = self.core.form_lines(e, mode="full")
                blocks.append(
                    f"{e['lemma']}（{POS_ZH.get(e['pos'], e['pos'])}）\n"
                    + "\n".join(lines)
                )
            text = "\n\n".join(blocks) or "没有找到变形表。"
            if len(text) > 1200:
                text = text[:1200] + "…\n（表太长已截断）"
            yield event.plain_result(text)
        except Exception as e:  # noqa: BLE001 —— 插件不能因单次异常崩溃
            logger.error(f"[ru_dict] 变形查询异常: {e}")
            yield event.plain_result("查询出错了，稍后再试。")

    # ---------- 给大模型用的函数工具 ----------

    @filter.llm_tool(name="ru_lookup")
    async def tool_ru_lookup(
        self, event: AstrMessageEvent, word: str, want_forms: bool = True
    ):
        """查询俄语单词或短语：词义、词性、重音与 IPA、体（完成体/未完成体）、变格或变位、例句。
        用户问俄语词是什么意思、复数怎么变、过去式怎么说、怎么读、完成体是什么时使用。

        Args:
            word(string): 要查的俄语单词或短语，原形或变形都可以，例如 книга / книгами / шёл
            want_forms(boolean): 是否附带变格变位摘要，默认 true
        """
        if not self.core:
            yield event.plain_result("词库未加载，无法查询。请管理员先建库。")
            return
        try:
            res = self.core.lookup(word)
        except Exception as e:  # noqa: BLE001 —— 插件不能因单次异常崩溃
            logger.error(f"[ru_dict] 工具查询异常: {e}")
            yield event.plain_result("查询失败，稍后再试。")
            return
        if res["kind"] in ("none", "empty"):
            sug = res.get("suggestions") or []
            if sug:
                items = "、".join(s["lemma"] for s in sug)
                yield event.plain_result(
                    f"词库未收录「{word}」。可能想查：{items}。请勿自行编造释义。"
                )
            else:
                yield event.plain_result(
                    f"词库未收录「{word}」。请勿自行编造释义，可建议用户换拼写。"
                )
            return
        yield event.plain_result(self._tool_text(res, want_forms))

    def _tool_text(self, res: dict, want_forms: bool = True) -> str:
        """把查询结果压成适合喂给大模型的结构化文本。"""
        out = []
        if res.get("matched"):
            m = res["matched"][0]
            out.append(
                f"用户输入「{m['form']}」是屈折形式（{tags_zh(m['tags'])}），原形如下"
            )
        for e in res["entries"][:2]:
            meta = [e["lemma"], e["lemma_plain"], POS_ZH.get(e["pos"], e["pos"])]
            if e["ipa"]:
                meta.append(f"IPA {e['ipa']}")
            if e["aspect"]:
                meta.append("未完成体" if e["aspect"] == "imperfective" else "完成体")
            if e["partner"]:
                meta.append(f"体对 {e['partner']}")
            out.append("词条｜" + "｜".join(meta))
            out.append("英文释义｜" + "；".join(e["glosses"][:4]))
            if want_forms:
                lines = self.core.form_lines(e)
                if lines:
                    out.append("变形｜" + " ／ ".join(lines))
            if e["examples"]:
                ex = e["examples"][0]
                out.append(
                    "例句｜" + ex["ru"] + (f"（{ex['en']}）" if ex.get("en") else "")
                )
        out.append(
            "来源｜Wiktionary/Kaikki。请只使用以上信息作答，不要补充未给出的释义或变形。"
        )
        return "\n".join(out)

    async def _handle_ru(self, event: AstrMessageEvent):
        if not self.core:
            yield event.plain_result(self._missing_db_msg())
            return
        word = self._strip_cmd(event, CMD_RU)
        if not word:
            yield event.plain_result(
                "俄语词典\n"
                "· /ru книга —— 查单词\n"
                "· /ru до свидания —— 查短语\n"
                "· /变形 книга —— 完整变格表\n"
                "直接发变形过的词也能查到原形，比如 /ru книгами"
            )
            return

        try:
            res = self.core.lookup(word)
        except Exception as e:  # noqa: BLE001 —— 插件不能因单次异常崩溃
            logger.error(f"[ru_dict] 查询异常: {e}")
            yield event.plain_result("查询出错了，稍后再试。")
            return

        if res["kind"] in ("none", "empty"):
            yield event.plain_result(render(res, core=self.core))
            return

        zh = await self._translate(event, res["entries"])
        text = render(res, zh_map=zh, core=self.core)

        if self.config.get("render_mode", "text") == "image":
            try:
                url = await self.html_render(
                    load_card_template(),
                    {
                        "text": text,
                        "lemma": res["entries"][0]["lemma"] if res["entries"] else "",
                    },
                )
                yield event.image_result(url)
                return
            except Exception as e:  # noqa: BLE001 —— 插件不能因单次异常崩溃
                logger.warning(f"[ru_dict] 图片渲染失败，回退文本: {e}")
        yield event.plain_result(text)

    async def terminate(self):
        """插件卸载时释放数据库连接。"""
        try:
            if self.core:
                self.core._conn.close()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[ru_dict] 关闭数据库连接时出错: {e}")


_TEMPLATE_CACHE: str | None = None


def load_card_template() -> str:
    global _TEMPLATE_CACHE
    if _TEMPLATE_CACHE is None:
        path = os.path.join(os.path.dirname(__file__), "card.html")
        with open(path, "r", encoding="utf-8") as f:
            _TEMPLATE_CACHE = f.read()
    return _TEMPLATE_CACHE
