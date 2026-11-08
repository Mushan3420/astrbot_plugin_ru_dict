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
import ipaddress
import json
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

try:  # AstrBot 4.x 的插件页接口用 request 代理读请求体；老版本没有就按无参数处理
    from astrbot.api.web import request as web_request
except Exception:  # noqa: BLE001 —— 缺这个模块不能让插件加载失败
    web_request = None

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
MANIFEST_ASSET = (
    "dict_manifest.json"  # 词库版本清单，与 ru_dict.db.gz 放在同一个 Release
)
DEFAULT_MANIFEST_URL = (
    f"https://github.com/Mushan3420/{PLUGIN_NAME}/releases/latest/download/"
    f"{MANIFEST_ASSET}"
)
MAX_MIRROR_LEN = 220  # 加速地址模板的长度上限，防止误填一长串东西


def _mirror_to_url(template: str, target: str) -> str:
    """把加速地址模板拼成实际下载地址。

    支持三种写法：`https://mirror/{url}` 占位符、`https://mirror` 前缀、
    直接给完整地址。
    """
    t = template.strip()
    if "{url}" in t:
        return t.replace("{url}", target)
    if t.endswith(DB_ASSET):
        return t
    return t.rstrip("/") + "/" + target


def _mirror_is_safe(template: str) -> bool:
    """校验加速地址，避免把服务器带去打内网（SSRF）。"""
    t = template.strip()
    if not t or len(t) > MAX_MIRROR_LEN:
        return False
    if "{url}" in t:
        t = t.replace("{url}", "https://github.com/x/y")
    m = re.match(r"^(https?)://([^/\s:?#]+)", t, re.IGNORECASE)
    if not m:
        return False
    host = m.group(2).lower()
    if host in ("localhost", "0.0.0.0"):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "." in host  # 必须是域名，不是裸单词
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
    )


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
    "1.1.1",
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
        self._pause_requested = False
        self._partial_meta: dict = {}
        self._pending_manifest: dict = {}  # 本次下载对应的版本清单，装完写进 .meta.json
        self._manifest_cache: tuple | None = (
            None  # (时间戳, 清单, 线路标签)，5 分钟内复用
        )
        self._db_sha_cache: tuple | None = None
        self._dl: dict = {
            "state": "idle",
            "bytes": 0,
            "total": 0,
            "message": "",
            "speed": 0.0,
            "t0": 0.0,
            "source": "",
        }
        self._clean_temp()
        self._register_pages(context)

    def _clean_temp(self) -> None:
        """清掉上次解压残留；.download.gz 和 .download.json 要留着续传。"""
        _, tmp, meta = self._dl_paths()
        self._remove(tmp)
        if os.path.exists(meta) and not os.path.exists(self.db_path + ".download.gz"):
            self._remove(meta)

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
            register(
                f"/{PLUGIN_NAME}/mirror", self.page_set_mirror, ["POST"], "设置镜像"
            )
            register(f"/{PLUGIN_NAME}/pause", self.page_pause, ["POST"], "暂停下载")
            register(f"/{PLUGIN_NAME}/cancel", self.page_cancel, ["POST"], "取消下载")
            register(f"/{PLUGIN_NAME}/check", self.page_check, ["GET"], "检查词库版本")
        except Exception as e:  # noqa: BLE001
            logger.error(f"[ru_dict] 注册页面接口失败: {e}")

    def _db_url(self) -> str:
        return str(self.config.get("db_url") or DEFAULT_DB_URL).strip()

    # ---------- 版本管理 ----------

    def _manifest_url(self) -> str:
        """词库清单地址：默认跟 db_url 同目录的 dict_manifest.json。"""
        custom = str(self.config.get("db_manifest_url") or "").strip()
        if custom:
            return custom
        url = self._db_url()
        # 清单跟词库同目录：自建源、固定 tag、latest/download 三种都适用
        if "/" in url:
            return url.rsplit("/", 1)[0] + "/" + MANIFEST_ASSET
        return DEFAULT_MANIFEST_URL

    def _meta_path(self) -> str:
        """记录本机已装词库的版本信息，跟 db 放在一起。"""
        return self.db_path + ".meta.json"

    def _read_meta(self) -> dict:
        try:
            with open(self._meta_path(), encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_meta(self, info: dict) -> None:
        try:
            with open(self._meta_path(), "w", encoding="utf-8") as f:
                json.dump(info, f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.debug(f"[ru_dict] 词库版本信息写入失败: {e}")

    @staticmethod
    def _version_key(v: str) -> tuple[int, ...]:
        """把 2026.11.08 / v2.1 之类的版本串变成可比较的整数元组。"""
        parts = [int(x) for x in re.findall(r"\d+", str(v or ""))]
        return tuple(parts) or (0,)

    @staticmethod
    def _sha256(path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1048576), b""):
                h.update(chunk)
        return h.hexdigest()

    async def _fetch_manifest(
        self, page_proxy: str = "", use_cache: bool = True
    ) -> dict | None:
        """拉词库清单，走与词库相同的线路（镜像 → 页面代理 → 直连）。"""
        url = self._manifest_url()
        if not url:
            return None
        cached = self._manifest_cache
        if use_cache and cached and time.time() - cached[0] < 300 and cached[1]:
            return dict(cached[1], _source=cached[2])
        try:
            import aiohttp
        except ImportError:
            return None
        timeout = aiohttp.ClientTimeout(total=20, connect=10, sock_read=15)
        candidates = self._candidates(page_proxy, url)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            for label, murl in candidates:
                try:
                    async with sess.get(murl, allow_redirects=True) as resp:
                        if resp.status != 200:
                            logger.debug(f"[ru_dict] 清单 {label} 返回 {resp.status}")
                            continue
                        data = json.loads(await resp.text())
                        if not isinstance(data, dict) or not data.get("version"):
                            logger.debug(f"[ru_dict] 清单 {label} 内容不像清单")
                            continue
                        self._manifest_cache = (time.time(), data, label)
                        return dict(data, _source=label)
                except Exception as e:  # noqa: BLE001 —— 换下一条线路
                    logger.debug(f"[ru_dict] 清单 {label} 拉取失败: {e}")
        return None

    def _db_sha(self) -> str:
        """本机词库内容的 sha256（按 mtime+size 缓存，只在版本不一致时算一次）。"""
        if not os.path.exists(self.db_path):
            return ""
        st = os.stat(self.db_path)
        key = (st.st_mtime, st.st_size)
        if self._db_sha_cache and self._db_sha_cache[0] == key:
            return self._db_sha_cache[1]
        try:
            digest = self._sha256(self.db_path)
        except OSError as e:
            logger.debug(f"[ru_dict] 计算本机词库指纹失败: {e}")
            return ""
        self._db_sha_cache = (key, digest)
        return digest

    def _dict_version(self) -> dict:
        """本机已装词库的版本信息（旧版装的没有 meta，就报未知）。"""
        meta = self._read_meta()
        if not os.path.exists(self.db_path):
            return {"version": "", "installed_at": "", "entries": 0, "sha256": ""}
        return {
            "version": str(meta.get("version") or ""),
            "installed_at": str(meta.get("installed_at") or ""),
            "entries": int(meta.get("entries") or 0),
            "sha256": str(meta.get("sha256") or ""),
            "source": str(meta.get("source") or ""),
        }

    def _candidates(
        self, page_proxy: str = "", target: str = ""
    ) -> list[tuple[str, str]]:
        """按优先级给出下载线路：插件配置的镜像 → WebUI 里选的镜像 → 直连。

        `db_mirror` 是服务器侧固定配置（比如自建 OSS/反代），优先于页面上临时选中的。
        `target` 用于给别的文件（比如词库清单）套用同一组线路。
        """
        target = target or self._db_url()
        if not target:
            return []
        templates: list[str] = [
            t.strip()
            for t in re.split(r"[,\n]", str(self.config.get("db_mirror") or ""))
            if t.strip()
        ]
        if page_proxy.strip():
            templates.append(page_proxy.strip())

        out: list[tuple[str, str]] = []
        for t in templates:
            if not _mirror_is_safe(t):
                logger.warning(f"[ru_dict] 忽略不安全的加速地址: {t[:60]}")
                continue
            out.append((t.split("://", 1)[-1].split("/")[0], _mirror_to_url(t, target)))
        out.append(("直连 GitHub", target))
        return out

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
                "mirror": str(self.config.get("db_mirror") or ""),
                "partial": self._partial_info(),
                "version": self._dict_version(),
                "latest": (
                    {
                        "version": str(self._manifest_cache[1].get("version") or ""),
                        "entries": int(self._manifest_cache[1].get("entries") or 0),
                        "built_at": str(self._manifest_cache[1].get("built_at") or ""),
                    }
                    if self._manifest_cache
                    else None
                ),
            }
        )

    async def page_check(self):
        """检查远端词库版本：对比本机版本，告诉页面要不要更新。"""
        manifest = await self._fetch_manifest(await self._page_proxy(), use_cache=False)
        current = self._dict_version()
        if not manifest:
            return jsonify(
                {
                    "ok": False,
                    "current": current["version"],
                    "message": "拉不到词库清单（dict_manifest.json），可以换条加速线路或直连再试",
                }
            )
        latest = str(manifest.get("version") or "")
        newer = bool(latest) and (
            not current["version"]
            or self._version_key(latest) > self._version_key(current["version"])
        )
        adopted = False
        # 版本号对不上、但文件内容与远端完全相同（比如升级前装的库），
        # 直接补一条版本记录，不必再下 60MB
        if newer and manifest.get("db_sha256") and os.path.exists(self.db_path):
            local_sha = self._db_sha()
            if local_sha and local_sha == str(manifest.get("db_sha256")):
                prev = self._read_meta()
                self._write_meta(
                    {
                        "version": latest,
                        "entries": int(
                            manifest.get("entries") or current["entries"] or 0
                        ),
                        "bytes": os.path.getsize(self.db_path),
                        "sha256": str(manifest.get("sha256") or ""),
                        "db_sha256": local_sha,
                        "installed_at": str(prev.get("installed_at") or ""),
                        "source": str(prev.get("source") or "指纹校验一致"),
                    }
                )
                adopted = True
                newer = False
        self._pending_manifest = manifest
        if adopted:
            msg = f"本机词库与远端 {latest} 完全一致，已补记版本号，不用重新下载"
        elif newer:
            msg = f"发现新版本 {latest}"
        else:
            msg = f"已是最新版本（{latest or current['version']}）"
        return jsonify(
            {
                "ok": True,
                "current": current["version"],
                "latest": latest,
                "update_available": newer,
                "manifest": manifest,
                "message": msg,
            }
        )

    async def page_progress(self):
        d = dict(self._dl)
        if d["state"] == "running" and d["total"]:
            d["percent"] = round(d["bytes"] * 100 / d["total"], 1)
        return jsonify({"ok": True, "download": d})

    @staticmethod
    async def _page_proxy() -> str:
        """取 WebUI 页面传来的加速地址（页面从 localStorage 读 AstrBot 的设置）。"""
        if web_request is None:
            return ""
        try:
            payload = await web_request.json(default={}) or {}
            if isinstance(payload, dict) and payload.get("proxy"):
                return str(payload["proxy"])
            return str(web_request.query.get("proxy") or "")
        except Exception as e:  # noqa: BLE001 —— 读不到就当没开加速，不能因此下载失败
            logger.debug(f"[ru_dict] 读取页面加速地址失败: {e}")
            return ""

    # ---------- 下载 ----------

    def _dl_paths(self) -> tuple[str, str, str]:
        """（半截压缩包，解压临时文件，续传元信息）。"""
        return (
            self.db_path + ".download.gz",
            self.db_path + ".download",
            self.db_path + ".download.json",
        )

    @staticmethod
    def _remove(path: str) -> None:
        for f in (path, path + "-wal", path + "-shm"):
            try:
                os.remove(f)
            except OSError:
                pass

    def _save_partial(self, meta_path: str, extra: dict) -> None:
        data = dict(self._partial_meta)
        data.update(extra)
        data["db_url"] = self._db_url()
        data["ts"] = time.time()
        try:
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except OSError as e:
            logger.debug(f"[ru_dict] 续传信息写入失败: {e}")

    def _resume_info(self, gz: str, meta: str, candidates: list[tuple[str, str]]):
        """返回（从第几条线路继续，已有字节数，线路标签）。无可续传进度返回 (0, 0, "")。"""
        if not os.path.exists(gz):
            return 0, 0, ""
        try:
            with open(meta, encoding="utf-8") as f:
                saved = json.load(f)
        except (OSError, ValueError):
            saved = {}
        size = os.path.getsize(gz)
        if not saved or saved.get("db_url") != self._db_url() or size <= 0:
            # 来源变了或信息不全，半截包作废
            self._remove(gz)
            self._remove(meta)
            return 0, 0, ""
        urls = [u for _, u in candidates]
        idx = urls.index(saved["url"]) if saved.get("url") in urls else 0
        self._partial_meta = saved
        return idx, size, candidates[idx][0]

    def _partial_info(self) -> dict:
        gz, _, _ = self._dl_paths()
        exists = os.path.exists(gz)
        return {"exists": exists, "bytes": os.path.getsize(gz) if exists else 0}

    async def page_set_mirror(self):
        """页面里选的加速线路存进插件配置 db_mirror，下次启动仍然生效。"""
        if web_request is None:
            return jsonify(
                {
                    "ok": False,
                    "message": "当前 AstrBot 版本拿不到页面请求，请改在插件配置里填 db_mirror",
                }
            )
        try:
            payload = await web_request.json(default={}) or {}
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "message": f"读取请求失败：{e}"})
        if not isinstance(payload, dict):
            payload = {}
        raw = str(payload.get("mirror") or "").strip()
        mirrors = [m.strip() for m in raw.replace("\n", ",").split(",") if m.strip()]
        for m in mirrors:
            if not _mirror_is_safe(m):
                return jsonify(
                    {
                        "ok": False,
                        "message": f"这个地址不能用：{m}，只允许公网 http/https",
                    }
                )
        persisted = False
        try:
            self.config["db_mirror"] = ",".join(mirrors)
            save = getattr(self.config, "save_config", None)
            if callable(save):
                save()
                persisted = True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[ru_dict] 保存镜像设置失败: {e}")
            return jsonify({"ok": False, "message": f"保存配置失败：{e}"})
        base = "已保存，下载会走这条线路" if mirrors else "已关闭镜像，直连 GitHub"
        if self._dl_task and not self._dl_task.done():
            base += "；当前这次下载仍走它开始时的线路，下次下载生效"
        tip = "" if persisted else "（当前 AstrBot 写不回配置文件，重启后需重设）"
        return jsonify({"ok": True, "message": base + tip, "mirror": ",".join(mirrors)})

    async def page_download(self):
        if self._dl_task and not self._dl_task.done():
            return jsonify({"ok": False, "message": "已经在下载了，请稍等"})
        if not self._db_url():
            return jsonify(
                {
                    "ok": False,
                    "message": "没有配置下载地址，请在插件配置里把 db_url 填成你的 Release 地址",
                }
            )
        candidates = self._candidates(await self._page_proxy())
        updating = os.path.exists(self.db_path)
        gz, _, meta = self._dl_paths()
        start, offset, label = self._resume_info(gz, meta, candidates)
        self._pause_requested = False
        self._dl = {
            "state": "running",
            "bytes": offset,
            "total": int(self._partial_meta.get("total") or 0),
            "message": (
                f"继续下载，已有 {offset / 1048576:.1f} MB"
                if offset
                else ("开始更新词库…" if updating else "开始下载…")
            ),
            "speed": 0.0,
            "t0": time.time(),
            "source": label,
            "resumed": bool(offset),
            "trace": [],
        }
        self._dl_task = asyncio.create_task(
            self._download_db(candidates, start, offset)
        )
        return jsonify({"ok": True, "message": "继续下载" if offset else "已开始下载"})

    async def page_pause(self):
        """暂停下载：保留半截压缩包和续传信息，之后可以接着下。"""
        task = self._dl_task
        if not task or task.done():
            return jsonify({"ok": False, "message": "当前没有在下载"})
        self._pause_requested = True
        self._dl.update(message="已暂停，正在保存进度…", speed=0.0)
        task.cancel()
        return jsonify({"ok": True, "message": "已暂停"})

    async def page_cancel(self):
        """取消下载：连半截文件和续传信息一起清掉，旧词库不动。"""
        task = self._dl_task
        if not task or task.done():
            return jsonify({"ok": False, "message": "当前没有在下载"})
        self._pause_requested = False
        self._dl.update(message="已取消，正在清理临时文件…", speed=0.0)
        task.cancel()
        return jsonify({"ok": True, "message": "已取消"})

    async def _download_db(
        self, candidates: list[tuple[str, str]], start: int = 0, offset: int = 0
    ) -> None:
        """带断点续传的下载：Range + If-Range，逐条线路尝试，最后校验并原子替换。

        换线路时**保留**已下的半截包。镜像与 GitHub 原址指向同一个对象，ETag 相同
        （实测都是 "0x8DF3D961930DDFD"），所以带着 If-Range 去下一条线路，服务器会
        返回 206 让我们接着下，而不是从头来。
        """
        gz, tmp, meta = self._dl_paths()
        keep = False  # 失败/暂停时是否保留半截包
        used = ""
        try:
            import aiohttp

            os.makedirs(
                os.path.dirname(os.path.abspath(self.db_path)) or ".", exist_ok=True
            )
            timeout = aiohttp.ClientTimeout(total=None, connect=15, sock_read=60)
            last_err = "没有可用的下载线路"

            for i in range(start, len(candidates)):
                label, url = candidates[i]
                # 每条线路都从磁盘现状续传，跨线路不丢进度
                off = os.path.getsize(gz) if os.path.exists(gz) else 0
                if off and not self._partial_meta.get("etag"):
                    off = 0  # 没有 ETag 就没法保证是同一个文件，只能整包下
                attempts = 0
                completed = False
                while True:
                    headers: dict[str, str] = {}
                    if off > 0:
                        headers["Range"] = f"bytes={off}-"
                        if self._partial_meta.get("etag"):
                            headers["If-Range"] = str(self._partial_meta["etag"])
                    self._dl.update(
                        message=(
                            f"从 {off / 1048576:.1f} MB 处继续（{label}）"
                            if off
                            else f"正在连接 {label}…"
                        ),
                        source=label,
                        bytes=off,
                        t0=time.time(),
                    )
                    done = off
                    try:
                        async with (
                            aiohttp.ClientSession(timeout=timeout) as sess,
                            sess.get(
                                url, headers=headers, allow_redirects=True
                            ) as resp,
                        ):
                            if resp.status == 416 and off > 0:
                                known = int(self._partial_meta.get("total") or 0)
                                if known and off >= known:
                                    # 半截包其实已经齐了，交给解压校验
                                    completed = True
                                    break
                                if attempts == 0:
                                    self._remove(gz)
                                    off = 0
                                    attempts += 1
                                    continue
                                raise RuntimeError("HTTP 416，续传位置无效")
                            if resp.status not in (200, 206):
                                raise RuntimeError(f"HTTP {resp.status}")
                            if resp.status == 200 and off > 0:
                                off = 0  # 服务器没按 Range 给，只能从头下
                            body = int(resp.headers.get("Content-Length") or 0)
                            total = off + body if resp.status == 206 else body
                            self._partial_meta = {
                                "url": url,
                                "label": label,
                                "etag": resp.headers.get("ETag") or "",
                                "last_modified": resp.headers.get("Last-Modified")
                                or "",
                                "total": total,
                            }
                            self._dl.update(total=total, bytes=off, t0=time.time())
                            last_save = time.time()
                            # 本地磁盘写 256KB 块，耗时远小于一次网络 await，同步写法更简单
                            with open(gz, "ab" if off else "wb") as f:  # noqa: ASYNC230
                                try:
                                    async for chunk in resp.content.iter_chunked(
                                        262144
                                    ):
                                        f.write(chunk)
                                        done += len(chunk)
                                        elapsed = time.time() - self._dl["t0"]
                                        self._dl.update(
                                            bytes=done,
                                            speed=done / elapsed if elapsed > 0 else 0,
                                            message=f"已下载 {done / 1048576:.1f} MB（{label}）",
                                        )
                                        if time.time() - last_save > 3:
                                            self._save_partial(meta, {"bytes": done})
                                            last_save = time.time()
                                except Exception as tail:
                                    # 收尾时对端断开，但字节数已经齐了：这不是失败
                                    if not (total and done >= total):
                                        raise
                                    logger.warning(
                                        f"[ru_dict] {label} 收尾时连接断开，但字节已齐"
                                        f"（{done}/{total}）：{tail}"
                                    )
                            self._save_partial(meta, {"bytes": done})
                            if total and done < total:
                                raise RuntimeError(f"只收到 {done}/{total} 字节")
                        completed = True
                        break
                    except Exception as e:  # noqa: BLE001 —— 单条线路失败就换下一条
                        last_err = f"{label}：{e}"
                        logger.warning(f"[ru_dict] 线路 {label} 失败：{e}")
                        self._dl.setdefault("trace", []).append(last_err)
                        if i + 1 < len(candidates):
                            nxt = candidates[i + 1][0]
                            hint = (
                                f"，已下的 {os.path.getsize(gz) / 1048576:.1f} MB 会带着续传"
                                if os.path.exists(gz)
                                else ""
                            )
                            self._dl.update(
                                message=f"{label} 失败（{e}），改用 {nxt} 接着下{hint}"
                            )
                        else:
                            keep = True  # 最后一条也失败，留着半截包下次续传
                        break

                if completed:
                    used = label
                    break

            if not used:
                kept = os.path.getsize(gz) if os.path.exists(gz) else 0
                if kept > 0:
                    keep = True
                    self._save_partial(meta, {"bytes": kept})
                    raise RuntimeError(
                        f"所有下载线路都失败了（{last_err}）；"
                        f"已保留 {kept / 1048576:.1f} MB，可点「继续下载」重试"
                    )
                raise RuntimeError(f"所有下载线路都失败了（{last_err}）")

            manifest = self._pending_manifest or {}
            if not manifest:
                try:
                    manifest = await self._fetch_manifest(use_cache=False) or {}
                except Exception as e:  # noqa: BLE001 —— 拉不到清单不影响安装
                    logger.debug(f"[ru_dict] 安装前拉清单失败: {e}")
                    manifest = {}

            self._dl["message"] = "解压中…"
            try:
                with gzip.open(gz, "rb") as src, open(tmp, "wb") as dst:  # noqa: ASYNC230
                    shutil.copyfileobj(src, dst, 1048576)
                self._dl["message"] = "校验中…"
                n = self._count_entries(tmp)
                if n < MIN_ENTRIES:
                    raise RuntimeError(f"词库不完整，只读到 {n} 个词条")
                digest = self._sha256(gz)
                want = str(manifest.get("sha256") or "") if manifest else ""
                if want and digest != want:
                    raise RuntimeError(
                        f"下载文件的 SHA256 与清单不一致（{digest[:12]}…≠{want[:12]}…），"
                        "可能下到了半截或旧文件"
                    )
            except Exception:
                keep = False  # 包本身有问题，留着续传也没用
                raise

            try:
                self._close_core()  # 先断开旧连接，Windows 上才换得动文件
                os.replace(tmp, self.db_path)
            finally:
                self._entries_cache = None
                self._reload_core()
            prev = self._read_meta()
            self._write_meta(
                {
                    "version": str(
                        manifest.get("version") or prev.get("version") or "未知"
                    ),
                    "entries": n,
                    "bytes": os.path.getsize(self.db_path),
                    "sha256": digest,
                    "db_sha256": self._sha256(self.db_path),
                    "installed_at": time.strftime("%Y-%m-%d %H:%M"),
                    "source": used,
                }
            )
            self._pending_manifest = {}
            keep = False
            self._dl.update(
                state="done", message=f"完成，共 {n} 个词条（线路：{used}）"
            )
            logger.info(
                f"[ru_dict] 词库下载完成，{n} 个词条 -> {self.db_path}（{used}）"
            )

        except asyncio.CancelledError:
            size = os.path.getsize(gz) if os.path.exists(gz) else 0
            if self._pause_requested:
                self._save_partial(meta, {"bytes": size})
                keep = True
                self._dl.update(
                    state="paused",
                    speed=0.0,
                    message=f"已暂停，已缓存 {size / 1048576:.1f} MB，可点「继续下载」",
                )
                logger.info(f"[ru_dict] 下载已暂停，缓存 {size} 字节")
            else:
                self._dl.update(
                    state="cancelled",
                    speed=0.0,
                    message="已取消，临时文件已清理，旧词库未改动",
                )
                logger.info("[ru_dict] 下载已被用户取消")
        except Exception as e:  # noqa: BLE001 —— 下载失败要让用户看到原因，不能静默
            logger.error(f"[ru_dict] 词库下载失败: {e}")
            self._dl.update(state="error", message=str(e))
        finally:
            self._remove(tmp)
            if not keep:
                self._remove(gz)
                self._remove(meta)

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
        """插件卸载时释放数据库连接，并停掉还在跑的下载任务。"""
        if self._dl_task and not self._dl_task.done():
            self._dl_task.cancel()
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
