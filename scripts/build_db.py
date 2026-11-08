#!/usr/bin/env python3
"""
俄语词库建库脚本：Kaikki(ruWiktionary/enWiktionary 抽取) JSONL -> SQLite

数据来源：https://kaikki.org/dictionary/Russian/kaikki.org-dictionary-Russian.jsonl.gz
许可证：CC BY-SA 4.0（Wiktionary 派生数据），分发时请保留署名。

用法：
    python3 build_db.py --input ru.jsonl.gz --output ru_dict.db
    python3 build_db.py --input data/ --output ru_dict.db      # 目录下所有 .jsonl
"""

import argparse
import gzip
import json
import re
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path

STRESS_MARKS = ("\u0301", "\u0300")  # 组合重音/次重音符号
FORM_OF_RE = re.compile(
    r"^.+?\bof\s+[\u0400-\u04FF]"
)  # "plural of книга" 这类屈折形式条目  # 组合重音/次重音符号
SKIP_FORM_TAGS = {
    "romanization",
    "table-tags",
    "inflection-template",
    "class",
    "canonical",
}
NOISE_FORMS = {"-", "no-table-tags", "table-tags"}

POS_ZH = {
    "noun": "名词",
    "verb": "动词",
    "adj": "形容词",
    "adv": "副词",
    "intj": "感叹词",
    "prep": "前置词",
    "conj": "连词",
    "pron": "代词",
    "num": "数词",
    "particle": "语气词",
    "phrase": "短语",
    "name": "专名",
    "abbrev": "缩写",
    "prefix": "前缀",
}


def strip_stress(s: str) -> str:
    """去掉重音符号，保留 ё"""
    for m in STRESS_MARKS:
        s = s.replace(m, "")
    return unicodedata.normalize("NFC", s)


def normalize(s: str) -> str:
    """查询键归一化：小写、е/ё 统一、去重音、去标点、压缩空格"""
    s = strip_stress(s).lower().replace("ё", "е")
    s = re.sub(r"[^\w\s\-]", " ", s, flags=re.UNICODE)  # \w 覆盖西里尔字母
    s = re.sub(r"\s+", " ", s).strip()
    return s


def iter_lines(path: Path):
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
            for line in f:
                yield line
    else:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                yield line


def parse_record(d: dict, max_senses: int = 8, max_examples: int = 3):
    """把一条 Kaikki 记录转成 (entry, forms, examples)。不合法返回 None。"""
    if d.get("lang_code") != "ru":
        return None
    word = (d.get("word") or "").strip()
    if not word or any(ch in word for ch in "{}[]"):
        return None

    pos = d.get("pos") or ""
    forms = d.get("forms") or []

    canonical = word
    aspect = None
    partner = None
    lookup_forms = []  # (form, tags)

    for f in forms:
        val = (f.get("form") or "").strip()
        tags = [t for t in (f.get("tags") or []) if isinstance(t, str)]
        tagset = set(tags)
        if not val or val in NOISE_FORMS:
            continue
        if "canonical" in tagset:
            canonical = val
            if "perfective" in tagset:
                aspect = "perfective"
            elif "imperfective" in tagset:
                aspect = "imperfective"
            continue
        if tagset & SKIP_FORM_TAGS:
            continue
        if "perfective" in tagset and "dated" not in tagset:
            if normalize(val) != normalize(canonical):
                partner = val
            continue
        if "imperfective" in tagset and "dated" not in tagset:
            if normalize(val) != normalize(canonical):
                partner = val
            continue
        # 只看真正承载词形变化的形式
        if tagset & {
            "nominative",
            "genitive",
            "dative",
            "accusative",
            "instrumental",
            "prepositional",
            "vocative",
            "plural",
            "singular",
            "short-form",
            "imperative",
            "present",
            "past",
            "future",
            "participle",
            "adverbial",
            "comparative",
            "superlative",
            "alternative",
        }:
            lookup_forms.append((val, tags))

    ipa = None
    audio = None
    for snd in d.get("sounds") or []:
        if not ipa and snd.get("ipa"):
            ipa = snd["ipa"]
        if not audio and (snd.get("mp3_url") or snd.get("ogg_url")):
            audio = snd.get("mp3_url") or snd.get("ogg_url")

    glosses, examples = [], []
    is_form = 0
    for sen in (d.get("senses") or [])[:max_senses]:
        if sen.get("glosses"):
            glosses.append("; ".join(sen["glosses"]))
        if glosses and not is_form and FORM_OF_RE.match(glosses[0]):
            is_form = 1  # 该条目本身是某个词的屈折形式，排序时靠后
        for ex in (sen.get("examples") or [])[:max_examples]:
            ru = strip_stress(ex.get("text") or "")
            en = ex.get("english") or ex.get("translation") or ""
            if ru and ":" not in ru[:12] and len(ru) < 160:
                examples.append({"ru": ru, "en": en})
        if len(examples) >= max_examples:
            break

    if not glosses:
        return None

    entry = {
        "lemma": canonical,  # 带重音
        "lemma_plain": strip_stress(canonical),
        "norm": normalize(canonical),
        "pos": pos,
        "ipa": ipa,
        "audio": audio,
        "aspect": aspect,
        "partner": strip_stress(partner) if partner else None,
        "glosses": glosses,
        "examples": examples[:max_examples],
        "is_form": is_form,
    }
    return entry, lookup_forms


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE entry(
    id INTEGER PRIMARY KEY,
    lemma TEXT NOT NULL,
    lemma_plain TEXT NOT NULL,
    norm TEXT NOT NULL,
    pos TEXT,
    ipa TEXT,
    audio TEXT,
    aspect TEXT,
    partner TEXT,
    glosses TEXT,
    examples TEXT,
    is_form INTEGER DEFAULT 0
);
CREATE TABLE form(
    entry_id INTEGER NOT NULL,
    form TEXT NOT NULL,
    form_plain TEXT NOT NULL,
    norm TEXT NOT NULL,
    tags TEXT
);
CREATE INDEX idx_entry_norm ON entry(norm);
CREATE INDEX idx_form_norm ON form(norm);
CREATE TABLE zh_cache(
    key TEXT PRIMARY KEY,
    zh TEXT NOT NULL,
    created_at INTEGER
);
"""


def build(input_path: Path, output_path: Path):
    if output_path.exists():
        output_path.unlink()
    conn = sqlite3.connect(output_path)
    conn.executescript(SCHEMA)

    merged = {}
    total = kept = 0
    t0 = time.time()

    files = (
        [input_path] if input_path.is_file() else sorted(input_path.glob("*.jsonl*"))
    )
    for fp in files:
        for line in iter_lines(fp):
            total += 1
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            parsed = parse_record(d)
            if not parsed:
                continue
            entry, forms = parsed
            # 用带重音的原形做键：за́мок / замо́к 这类同形异重音词不能被合并
            key = (entry["lemma"].lower(), entry["pos"])
            if key in merged:
                old = merged[key]
                for g in entry["glosses"]:
                    if g not in old["entry"]["glosses"]:
                        old["entry"]["glosses"].append(g)
                old["entry"]["examples"].extend(entry["examples"])
                old["forms"].extend(forms)
            else:
                merged[key] = {"entry": entry, "forms": forms}
            kept += 1
            if total % 20000 == 0:
                print(
                    f"  ...{total} 行, {len(merged)} 词条", file=sys.stderr, flush=True
                )

    rows_e, rows_f = [], []
    for i, (_, payload) in enumerate(merged.items(), start=1):
        e = payload["entry"]
        rows_e.append(
            (
                i,
                e["lemma"],
                e["lemma_plain"],
                e["norm"],
                e["pos"],
                e["ipa"],
                e["audio"],
                e["aspect"],
                e["partner"],
                json.dumps(e["glosses"], ensure_ascii=False),
                json.dumps(e["examples"][:3], ensure_ascii=False),
                e["is_form"],
            )
        )
        seen = set()
        for val, tags in payload["forms"]:
            plain = strip_stress(val)
            norm = normalize(plain)
            if not norm or (norm, tuple(tags)) in seen:
                continue
            seen.add((norm, tuple(tags)))
            rows_f.append((i, val, plain, norm, ",".join(tags)))

    conn.executemany(
        "INSERT INTO entry(id,lemma,lemma_plain,norm,pos,ipa,audio,aspect,partner,glosses,examples,is_form)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        rows_e,
    )
    conn.executemany(
        "INSERT INTO form(entry_id,form,form_plain,norm,tags) VALUES(?,?,?,?,?)", rows_f
    )
    conn.commit()
    conn.executescript("ANALYZE;")
    conn.close()

    print(
        f"完成：原始 {total} 行，有效 {kept} 行，合并为 {len(merged)} 个词条，"
        f"{len(rows_f)} 条词形，耗时 {time.time() - t0:.1f}s -> {output_path}"
    )


def main():
    ap = argparse.ArgumentParser(description="Kaikki 俄语 JSONL -> SQLite")
    ap.add_argument("--input", required=True, help=".jsonl / .jsonl.gz 文件或目录")
    ap.add_argument("--output", default="ru_dict.db", help="输出 SQLite 路径")
    args = ap.parse_args()
    build(Path(args.input), Path(args.output))


if __name__ == "__main__":
    main()
