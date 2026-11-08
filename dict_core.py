#!/usr/bin/env python3
"""
俄语词典查询核心（纯标准库，无第三方依赖）

设计要点：
1. 归一化：小写 + ё→е + 去重音符号 + 去标点，解决用户不打重音、不打 ё 的问题
2. 两级索引：先按原形命中，再按屈折形式反查（книгами → книга）
3. 变形表按词性整理成人能读的短行，适配 QQ 纯文本
"""

import json
import re
import sqlite3
import threading
import unicodedata

STRESS_MARKS = ("\u0301", "\u0300")

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
    "postp": "后置词",
    "det": "限定词",
}

GENDER_ZH = {
    "masculine": "阳",
    "feminine": "阴",
    "neuter": "中",
    "animate": "有生",
    "inanimate": "无生",
}
CASE_ZH = {
    "nominative": "主",
    "genitive": "属",
    "dative": "与",
    "accusative": "宾",
    "instrumental": "工具",
    "prepositional": "前",
    "vocative": "呼",
}
TAG_ZH = {
    "singular": "单数",
    "plural": "复数",
    "present": "现在时",
    "past": "过去时",
    "future": "将来时",
    "imperative": "命令式",
    "short-form": "短尾",
    "comparative": "比较级",
    "superlative": "最高级",
    "infinitive": "不定式",
    "participle": "形动词",
    "adverbial": "副动词",
    "alternative": "异体",
    "first-person": "第一人称",
    "second-person": "第二人称",
    "third-person": "第三人称",
}
PERSON_ZH = {
    ("first-person", "singular"): "я",
    ("second-person", "singular"): "ты",
    ("third-person", "singular"): "он/она",
    ("first-person", "plural"): "мы",
    ("second-person", "plural"): "вы",
    ("third-person", "plural"): "они",
}


def tags_zh(tags) -> str:
    """把英文词形标签转成中文，用于「这是……的变形」这类提示。"""
    labels = [TAG_ZH.get(t) or CASE_ZH.get(t) or GENDER_ZH.get(t) for t in tags]
    return "、".join(x for x in labels if x)


def strip_stress(s: str) -> str:
    for m in STRESS_MARKS:
        s = s.replace(m, "")
    return unicodedata.normalize("NFC", s)


def normalize(s: str) -> str:
    s = strip_stress(s).lower().replace("ё", "е")
    s = re.sub(r"[^\w\s\-]", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip()


class RuDict:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    # ---------- 底层 ----------

    def _q(self, sql: str, *args):
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def _entry(self, row) -> dict:
        return {
            "id": row["id"],
            "lemma": row["lemma"],
            "lemma_plain": row["lemma_plain"],
            "pos": row["pos"],
            "ipa": row["ipa"],
            "audio": row["audio"],
            "aspect": row["aspect"],
            "partner": row["partner"],
            "glosses": json.loads(row["glosses"] or "[]"),
            "examples": json.loads(row["examples"] or "[]"),
            "is_form": row["is_form"],
        }

    def forms(self, entry_id: int) -> list[dict]:
        rows = self._q(
            "SELECT form, form_plain, tags FROM form WHERE entry_id=?", entry_id
        )
        return [
            {
                "form": r["form"],
                "plain": r["form_plain"],
                "tags": [t for t in (r["tags"] or "").split(",") if t],
            }
            for r in rows
        ]

    # ---------- 查询 ----------

    def lookup(self, query: str, limit: int = 3) -> dict:
        q = normalize(query)
        if not q:
            return {"kind": "empty"}

        rows = self._q(
            "SELECT * FROM entry WHERE norm=? ORDER BY is_form ASC, id ASC LIMIT ?",
            q,
            limit * 2,
        )
        if rows:
            entries = [self._entry(r) for r in rows]
            real = [e for e in entries if not e["is_form"]]
            return {"kind": "lemma", "query": q, "entries": (real or entries)[:limit]}

        # 屈折形式反查：книгами / шёл / красивого
        hits = self._q("SELECT entry_id, tags, form FROM form WHERE norm=? LIMIT 20", q)
        if hits:
            entries, matched = [], []
            ids = []
            for h in hits:
                if h["entry_id"] not in ids:
                    ids.append(h["entry_id"])
                    matched.append(
                        {"form": h["form"], "tags": (h["tags"] or "").split(",")}
                    )
            for eid in ids[:limit]:
                row = self._q("SELECT * FROM entry WHERE id=?", eid)
                if row:
                    entries.append(self._entry(row[0]))
            return {
                "kind": "form",
                "query": q,
                "entries": entries,
                "matched": matched[:3],
            }

        sug = self.suggest(q)
        return {"kind": "none", "query": q, "suggestions": sug}

    def suggest(self, q: str, limit: int = 6) -> list[dict]:
        if len(q) < 2:
            return []
        out, seen = [], set()

        def add(lemma, pos, norm):
            if norm in seen:
                return
            seen.add(norm)
            out.append({"lemma": lemma, "pos": pos})

        rows = self._q(
            "SELECT lemma, norm, pos FROM entry WHERE norm LIKE ? "
            "ORDER BY is_form ASC, length(norm) ASC LIMIT ?",
            q + "%",
            limit,
        )
        for r in rows:
            add(r["lemma"], r["pos"], r["norm"])
        if len(out) >= limit:
            return out[:limit]

        # 编辑距离兜底：只在前两个字母相同的候选里找，避免全表扫描
        import difflib

        pool = self._q(
            "SELECT DISTINCT lemma, norm, pos FROM entry WHERE norm LIKE ? "
            "ORDER BY is_form ASC LIMIT 300",
            q[:2] + "%",
        )
        cands = [r["norm"] for r in pool]
        close = difflib.get_close_matches(q, cands, n=limit - len(out), cutoff=0.5)
        for norm in close:
            r = next(x for x in pool if x["norm"] == norm)
            add(r["lemma"], r["pos"], r["norm"])
        return out[:limit]

    # ---------- 变形整理 ----------

    def form_lines(self, entry: dict, mode: str = "brief") -> list[str]:
        pos, fs = entry["pos"], self.forms(entry["id"])
        if not fs:
            return []

        BAD = ("dated", "alternative", "obsolete", "romanization")

        def clean(v: str) -> str:
            # 去掉词典里的"(по)краси́вее""(наи)краси́вейший"这类可选前缀
            return re.sub(r"^\((?:по|наи)\)", "", v)

        def pick(*tags, exclude=BAD):
            for f in fs:
                t = set(f["tags"])
                if any(x in t for x in exclude):
                    continue
                if set(tags) <= t:
                    return clean(f["form"])
            return None

        lines = []
        if pos == "noun":
            gender = " ".join(
                GENDER_ZH[g]
                for g in ("masculine", "feminine", "neuter")
                if any(g in f["tags"] for f in fs)
            )
            if gender:
                lines.append(f"性｜{gender}")
            for num, num_zh in (("singular", "单数"), ("plural", "复数")):
                cells = []
                for case, tag in CASE_ZH.items():
                    v = pick(case, num)
                    if v:
                        cells.append(f"{tag} {v}")
                if cells:
                    lines.append(f"{num_zh}｜" + " · ".join(cells))

        elif pos == "verb":
            present = []
            for (p_, n_), label in PERSON_ZH.items():
                v = pick(p_, n_, "present")
                if v:
                    present.append(f"{label} {v}")
            if present:
                lines.append("现在时｜" + " · ".join(present))
            past = []
            for tag, label in (
                ("masculine", "он"),
                ("feminine", "она"),
                ("neuter", "оно"),
                ("plural", "они"),
            ):
                v = pick(tag, "past")
                if v:
                    past.append(f"{label} {v}")
            if past:
                lines.append("过去时｜" + " · ".join(past))
            imp = pick("imperative", "second-person", "singular")
            imp_pl = pick("imperative", "second-person", "plural")
            if imp:
                lines.append("命令式｜" + imp + (f" / {imp_pl}" if imp_pl else ""))
            if mode == "full":
                fut = pick("future", "first-person", "singular") or pick(
                    "future", "singular", "third-person"
                )
                if fut:
                    lines.append("将来时｜" + fut + " …")
                act = []
                for tag, label in (("present", "主动现在"), ("past", "主动过去")):
                    v = pick(tag, "participle", "active")
                    if v:
                        act.append(f"{label} {v}")
                pas = []
                for tag, label in (("present", "被动现在"), ("past", "被动过去")):
                    v = pick(tag, "participle", "passive")
                    if v:
                        pas.append(f"{label} {v}")
                if act:
                    lines.append("形动词｜" + " · ".join(act))
                if pas:
                    lines.append("被动形动｜" + " · ".join(pas))
                adv = []
                for tag, label in (("present", "现在"), ("past", "过去")):
                    v = pick(tag, "adverbial", "participle")
                    if v:
                        adv.append(f"{label} {v}")
                if adv:
                    lines.append("副动词｜" + " · ".join(adv))

        elif pos == "adj":
            cells = []
            for tag, label in (
                ("masculine", "阳"),
                ("neuter", "中"),
                ("feminine", "阴"),
            ):
                v = pick(tag, "nominative", "singular") or pick(tag, "nominative")
                if v:
                    cells.append(f"{label} {v}")
            v = pick("nominative", "plural")
            if v:
                cells.append(f"复 {v}")
            if cells:
                lines.append("长尾｜" + " · ".join(cells))
            short = []
            for tag, label in (
                ("masculine", "阳"),
                ("neuter", "中"),
                ("feminine", "阴"),
            ):
                v = pick(
                    tag, "short-form", exclude=("dated", "alternative", "obsolete")
                )
                if v:
                    short.append(f"{label} {v}")
            v = pick(
                "plural", "short-form", exclude=("dated", "alternative", "obsolete")
            )
            if v:
                short.append(f"复 {v}")
            if short:
                lines.append("短尾｜" + " · ".join(short))
            if mode == "full":
                for tag, label in (
                    ("masculine", "阳性"),
                    ("feminine", "阴性"),
                    ("plural", "复数"),
                ):
                    cells = []
                    for case, tag2 in CASE_ZH.items():
                        v = pick(case, tag) if tag != "plural" else pick(case, "plural")
                        if v:
                            cells.append(f"{tag2} {v}")
                    if cells:
                        lines.append(f"{label}各格｜" + " · ".join(cells))

        comp = pick("comparative", exclude=("dated", "alternative", "obsolete"))
        if comp:
            lines.append(f"比较级｜{comp}")
        sup = pick("superlative", exclude=("dated", "alternative", "obsolete"))
        if sup:
            lines.append(f"最高级｜{sup}")
        return lines

    # ---------- 中文释义缓存 ----------

    def get_zh(self, key: str) -> list[str] | None:
        rows = self._q("SELECT zh FROM zh_cache WHERE key=?", key)
        return json.loads(rows[0]["zh"]) if rows else None

    def put_zh(self, key: str, zh: list[str]):
        import time

        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO zh_cache(key,zh,created_at) VALUES(?,?,?)",
                (key, json.dumps(zh, ensure_ascii=False), int(time.time())),
            )
            self._conn.commit()


# ---------- 文本渲染 ----------


def render(
    result: dict,
    zh_map: dict[int, list[str]] | None = None,
    show_forms: bool = True,
    core: RuDict | None = None,
    max_senses: int = 3,
) -> str:
    if result["kind"] == "empty":
        return "用法：/ru 单词或短语\n例如：/ru книга · /ru до свидания"

    if result["kind"] == "none":
        sug = result.get("suggestions") or []
        if sug:
            items = "、".join(
                f"{s['lemma']}（{POS_ZH.get(s['pos'], s['pos'])}）" for s in sug
            )
            return f"没查到「{result['query']}」。是不是想查：{items}"
        return f"没查到「{result['query']}」。试试只用原形，或换一个拼写。"

    out = []
    matched = result.get("matched")
    if matched:
        m = matched[0]
        tag_txt = tags_zh(m["tags"])
        out.append(f"「{m['form']}」是下列词条的变形（{tag_txt}）")

    for idx, e in enumerate(result["entries"][:2]):
        if idx > 0 or out:
            out.append("")
        head = e["lemma"]
        meta = [POS_ZH.get(e["pos"], e["pos"])]
        if e["ipa"]:
            meta.append(e["ipa"])
        if e["aspect"]:
            meta.append("未完成体" if e["aspect"] == "imperfective" else "完成体")
        if e["partner"]:
            meta.append(f"体对 {e['partner']}")
        out.append(f"{head}｜{' · '.join(meta)}")

        zh = (zh_map or {}).get(e["id"])
        glosses = zh if zh else e["glosses"]
        for i, g in enumerate(glosses[:max_senses], start=1):
            out.append(f" {i}. {g}")
        if zh is None and e["glosses"]:
            out.append(" （未接大模型，以上为英文释义）")

        if show_forms and core is not None:
            lines = core.form_lines(e)
            for ln in lines[:4]:
                out.append(f" {ln}")

        if e["examples"]:
            ex = e["examples"][0]
            ru = ex["ru"] if len(ex["ru"]) <= 80 else ex["ru"][:80] + "…"
            line = f" 例：{ru}"
            if ex.get("en"):
                en = ex["en"] if len(ex["en"]) <= 60 else ex["en"][:60] + "…"
                line += f"（{en}）"
            out.append(line)

    if len(out) > 22:
        out = out[:22] + ["…（内容较长，已截断）"]
    return "\n".join(out).strip()
