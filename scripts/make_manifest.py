#!/usr/bin/env python3
"""生成词库版本清单 dict_manifest.json，跟 ru_dict.db.gz 一起传进 GitHub Release。

用法：
    python scripts/make_manifest.py release/ru_dict.db.gz --version 2026.11.08 \
        --entries 438894 --forms 1584297

清单里带 version / entries / bytes / sha256 / built_at，插件页据此判断有没有新版本，
下载完还会用 sha256 校验文件是否完整。每次重建词库都要改 version，否则插件不会提示更新。
"""

import argparse
import gzip
import hashlib
import json
import os
import time


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1048576), b""):
            h.update(chunk)
    return h.hexdigest()


def db_digest(gz: str) -> tuple[str, int]:
    """解压后内容的 sha256 与字节数，用于判断「本机库与远端是不是同一份」。"""
    h = hashlib.sha256()
    size = 0
    with gzip.open(gz, "rb") as f:
        for chunk in iter(lambda: f.read(1048576), b""):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("gz", help="ru_dict.db.gz 路径")
    ap.add_argument(
        "--version", required=True, help="词库版本，建议用日期，如 2026.11.08"
    )
    ap.add_argument("--entries", type=int, default=0, help="词条数")
    ap.add_argument("--forms", type=int, default=0, help="词形数")
    ap.add_argument("--url", default="", help="词库直链，留空按默认 Release 地址填")
    ap.add_argument(
        "--out", default="", help="输出路径，默认与 gz 同目录的 dict_manifest.json"
    )
    a = ap.parse_args()

    url = a.url or (
        "https://github.com/Mushan3420/astrbot_plugin_ru_dict/releases/latest/download/"
        "ru_dict.db.gz"
    )
    db_sha, db_bytes = db_digest(a.gz)
    manifest = {
        "version": a.version,
        "entries": a.entries,
        "forms": a.forms,
        "bytes": os.path.getsize(a.gz),
        "sha256": sha256(a.gz),
        "db_bytes": db_bytes,
        "db_sha256": db_sha,
        "built_at": time.strftime("%Y-%m-%d"),
        "asset": "ru_dict.db.gz",
        "url": url,
    }
    out = a.out or os.path.join(
        os.path.dirname(os.path.abspath(a.gz)), "dict_manifest.json"
    )
    with open(out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"已写入 {out}")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
