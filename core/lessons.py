# -*- coding: utf-8 -*-
"""
教训（不依赖 AstrBot，可以单独测）：真错了就记一条，以后聊到类似的事提醒它「这类事你以前说错过」。

两种：
  事实：有人指出它说错了、判断模型核对过、它确实错了（入口 1）→ 什么情况、错的说法、对的说法、依据、谁指出的、话题词
  格式：验证程序那种场合连着没过、后来过了 / 停下了（入口 2）→ 回哪个号、他要的格式
同一件事合并（错的说法一样 / 话题词重合一半以上；格式按「同一个号 + 同一种格式」），次数 +1。
用到（给模型看过）或者又碰到算「新鲜」；half_days 半衰，lesson_days 天没碰到没用到就淡出（不给模型看，面板还看得到）；
最多 lesson_max 条，超了先删最淡的。私聊里记的事实教训（private_stay 开着时）只在那个私聊里用。
给模型的只有事实：哪天、什么情况、它说了什么、对的是什么、谁指出的。不写它该怎么说。
"""

import json
import os
import re
import time
import uuid
from pathlib import Path

from .text import keys as topic_keys
from .text import short

DEFAULTS = {
    "lesson_max": 80,  # 最多记几条（超了先删最淡的）
    "lesson_days": 45.0,  # 多少天没碰到 / 没用到就淡出
    "half_days": 30.0,  # 半衰期（天）
    "lesson_turn": 2,  # 每轮最多提醒几条
    "lesson_chars": 70,  # 每条最多几个字
    "lesson_daily_per_user": 5,  # 同一个人每天最多让它记几条新的（防有人刷）
    "private_stay": True,  # 私聊里记的事实教训只在那个私聊里用
}
EDITABLE = ("when", "wrong", "right", "basis", "topic")
LIMITS = {"when": 40, "wrong": 30, "right": 30, "basis": 50, "topic": 30}
HEAD_FACT = "\n# 这类事你以前说错过（系统记的 不是谁说的话）\n"
HEAD_FORMAT = "\n# 这个号以前的格式要求（系统记的 不是谁说的话）\n"
FORMAT_TAIL = {
    "pass": "那次多了别的字被判不对，改成这样以后过了",
    "stop": "那次多了别的字被判不对，连着没过、停下了",
}


def _day(t):
    return time.strftime("%Y-%m-%d", time.localtime(float(t or 0)))


def _md(t):
    return time.strftime("%m-%d", time.localtime(float(t or 0)))


class LessonStore:
    def __init__(self, path, cfg=None, clock=time.time, warn=None):
        self.path = Path(path)
        self.cfg = dict(DEFAULTS)
        self.set_cfg(cfg or {})
        self.clock = clock
        self.warn = warn or (lambda m: None)
        self.lessons = []
        self._adds = {}  # (日期, QQ) → 今天让它记了几条新的
        self.load()

    def set_cfg(self, cfg):
        for k, v in (cfg or {}).items():
            if k in DEFAULTS and v is not None:
                d = DEFAULTS[k]
                try:
                    if isinstance(d, bool):
                        self.cfg[k] = (
                            v
                            if isinstance(v, bool)
                            else str(v).strip().lower()
                            in ("1", "true", "yes", "on", "开")
                        )
                    else:
                        self.cfg[k] = type(d)(v)
                except (TypeError, ValueError):
                    pass

    # ───────── 存档 ─────────
    def load(self):
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            d = {}
        except (OSError, ValueError) as e:
            self.warn(f"教训存档读不了（当成空的，原文件没动）：{e!r}"[:200])
            d = {}
        ls = d.get("lessons") if isinstance(d, dict) else None
        self.lessons = [x for x in ls or [] if isinstance(x, dict) and x.get("id")]

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"lessons": self.lessons}, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
            return True
        except OSError as e:
            self.warn(f"教训存档写不进去：{e!r}"[:200])
            return False

    # ───────── 淡出 ─────────
    @staticmethod
    def _last(x):
        return max(
            float(x.get("last") or 0),
            float(x.get("used_t") or 0),
            float(x.get("t") or 0),
        )

    def weight(self, x, now=None):
        """越久没碰到 / 没给模型看过越淡（half_days 半衰）；被纠正过几次的重一点"""
        now = float(now if now is not None else self.clock())
        half = max(1.0, float(self.cfg["half_days"])) * 86400
        return 0.5 ** (max(0.0, now - self._last(x)) / half) * (
            1 + 0.3 * min(int(x.get("n") or 1) - 1, 3)
        )

    def faded(self, x, now=None):
        now = float(now if now is not None else self.clock())
        return now - self._last(x) > float(self.cfg["lesson_days"]) * 86400

    def _trim(self, now, keep=None):
        mx = max(1, int(self.cfg["lesson_max"]))
        if len(self.lessons) > mx:
            self.lessons.sort(
                key=lambda x: (
                    str(x.get("id")) == str(keep),
                    self.weight(x, now),
                    float(x.get("last") or 0),
                ),
                reverse=True,
            )
            del self.lessons[mx:]

    def _quota(self, uid, now):
        """同一个人今天还能让它记新的吗"""
        lim = int(self.cfg["lesson_daily_per_user"])
        if lim <= 0:
            return True
        k = (_day(now), str(uid))
        if len(self._adds) > 2000:
            self._adds = {a: b for a, b in self._adds.items() if a[0] == k[0]}
        return self._adds.get(k, 0) < lim

    def _count(self, uid, now):
        k = (_day(now), str(uid))
        self._adds[k] = self._adds.get(k, 0) + 1

    # ───────── 记 ─────────
    def add_fact(self, case, v, now=None):
        """核对说它真错了 → 记一条（同一件事合并）→ 教训的 id；这个人今天记满了 → None"""
        now = float(now if now is not None else self.clock())
        wrong = short(v.get("wrong") or case.get("her"), 30)
        right = short(v.get("right"), 30)
        when = short(case.get("prompt"), 40)
        ks = topic_keys(v.get("topic"), wrong, right, when)
        who = (case.get("name") or "有人") + ("（管理员）" if case.get("admin") else "")
        src = f"{who}指出、核对过"
        conv = str(case.get("conv") or "")
        private = bool(case.get("private"))
        hit = None
        for x in self.lessons:
            if x.get("kind", "事实") != "事实":
                continue
            if private and self.cfg["private_stay"] and x.get("conv") != conv:
                continue  # 私聊里的不跟别处的并（并了就带到别处去了）
            if x.get("private") and self.cfg["private_stay"] and x.get("conv") != conv:
                continue
            kx = set(x.get("keys") or [])
            same = bool(wrong) and short(x.get("wrong"), 30) == wrong
            near = (
                bool(kx and ks) and len(kx & ks) / max(1, min(len(kx), len(ks))) >= 0.5
            )
            if same or near:
                hit = x
                break
        if hit:
            hit.update(
                last=now,
                n=int(hit.get("n") or 1) + 1,
                keys=sorted(set(hit.get("keys") or []) | ks)[:40],
            )
            for f, val in (
                ("right", right),
                ("basis", short(v.get("basis"), 50)),
                ("topic", short(v.get("topic"), 30)),
            ):
                if val:
                    hit[f] = val
            if src not in str(hit.get("src") or ""):
                hit["src"] = short(f"{hit.get('src') or ''}；{src}".strip("；"), 80)
            uids = set(hit.get("uids") or [hit.get("uid")])
            uids.add(str(case.get("uid") or ""))
            hit["uids"] = sorted(u for u in uids if u)[:20]
            self._trim(now, hit["id"])
            self.save()
            return hit["id"]
        uid = str(case.get("uid") or "")
        if not self._quota(uid, now):
            return None
        x = {
            "id": uuid.uuid4().hex[:8],
            "kind": "事实",
            "t": now,
            "last": now,
            "n": 1,
            "used": 0,
            "used_t": 0,
            "when": when,
            "wrong": wrong,
            "right": right,
            "basis": short(v.get("basis"), 50),
            "topic": short(v.get("topic"), 30),
            "src": src,
            "conv": conv,
            "private": private,
            "uid": uid,
            "uids": [uid] if uid else [],
            "name": short(case.get("name"), 20),
            "case": str(case.get("id") or ""),
            "keys": sorted(ks)[:40],
        }
        self.lessons.append(x)
        self._count(uid, now)
        self._trim(now, x["id"])
        self.save()
        return x["id"]

    def add_format(self, conv, uid, name, con, src, now=None):
        """验证程序那种场合：连着没过以后过了（pass）/ 停下了（stop）→ 记「回这个号：他要的格式」→ id"""
        now = float(now if now is not None else self.clock())
        con = {k: v for k, v in (con or {}).items() if k in ("kind", "desc")} or None
        desc = (con or {}).get("desc") or "照他原话的格式"
        uid = str(uid)
        old = next(
            (
                x
                for x in self.lessons
                if x.get("kind") == "格式"
                and str(x.get("uid")) == uid
                and ((x.get("con") or {}).get("kind") == (con or {}).get("kind"))
            ),
            None,
        )
        tail = FORMAT_TAIL.get(src, "")
        if old:
            old.update(
                last=now,
                n=int(old.get("n") or 1) + 1,
                src=src,
                basis=tail or old.get("basis", ""),
            )
            if name:
                old["name"] = short(name, 20)
                old["when"] = short(f"回「{name}」（验证程序）", 40)
            if con:
                old["con"] = con
                old["right"] = short(desc, 30)
            self._trim(now, old["id"])
            self.save()
            return old["id"]
        x = {
            "id": uuid.uuid4().hex[:8],
            "kind": "格式",
            "t": now,
            "last": now,
            "n": 1,
            "used": 0,
            "used_t": 0,
            "when": short(f"回「{name or uid}」（验证程序）", 40),
            "wrong": "多了别的字 / 格式不对",
            "right": short(desc, 30),
            "basis": tail,
            "topic": "",
            "src": src,
            "conv": str(conv or ""),
            "private": False,
            "uid": uid,
            "uids": [uid],
            "name": short(name, 20),
            "con": con,
            "keys": [],
        }
        self.lessons.append(x)
        self._trim(now, x["id"])
        self.save()
        return x["id"]

    # ───────── 用 ─────────
    def match(self, conv, uid, texts=(), now=None, skip_case=None, n=None):
        """他这几句跟哪几条事实教训对得上（话题词至少对上两个；同一个人、同一个对话的排前面；淡出了的不算）"""
        now = float(now if now is not None else self.clock())
        q = topic_keys(*list(texts or [])[-3:])
        if not q:
            return []
        rows = []
        for x in self.lessons:
            if x.get("kind", "事实") != "事实" or self.faded(x, now):
                continue
            if skip_case and str(x.get("case")) == str(skip_case):
                continue
            if x.get("private") and self.cfg["private_stay"] and x.get("conv") != conv:
                continue
            hit = len(q & set(x.get("keys") or []))
            if hit < 2:
                continue
            score = (
                hit
                + (1.0 if str(uid) in set(x.get("uids") or [x.get("uid")]) else 0)
                + (0.5 if x.get("conv") == conv else 0)
                + self.weight(x, now)
            )
            rows.append((score, x))
        rows.sort(key=lambda r: -r[0])
        k = int(self.cfg["lesson_turn"] if n is None else n)
        return [x for _, x in rows[: max(0, k)]]

    def for_uid(self, uid, now=None):
        """回这个号时要带的格式教训（新的在前；淡出了的不带）"""
        now = float(now if now is not None else self.clock())
        ls = [
            x
            for x in self.lessons
            if x.get("kind") == "格式"
            and str(x.get("uid")) == str(uid)
            and not self.faded(x, now)
        ]
        return sorted(ls, key=lambda x: -float(x.get("last") or 0))

    def _line(self, x):
        lim = max(20, int(self.cfg["lesson_chars"]))
        if x.get("kind") == "格式":
            t = (
                f"{_md(x.get('t'))} {short(x.get('when'), 24)}：他要的是{short(x.get('right'), 20)}"
                + (f"（{short(x.get('basis'), 30)}）" if x.get("basis") else "")
            )
        else:
            t = (
                f"{_md(x.get('t'))} "
                + (f"{short(x.get('when'), 20)}：" if x.get("when") else "")
                + f"你说「{short(x.get('wrong'), 24)}」"
                + (
                    f"，对的是「{short(x.get('right'), 24)}」"
                    if x.get("right")
                    else "，被指出不对"
                )
                + f"（{short(x.get('src'), 16)}）"
            )
        return "- " + t[:lim]

    def text(self, rows):
        if not rows:
            return ""
        return HEAD_FACT + "\n".join(self._line(x) for x in rows) + "\n"

    def format_text(self, rows):
        if not rows:
            return ""
        return HEAD_FORMAT + "\n".join(self._line(x) for x in rows) + "\n"

    def mark_used(self, ids, now=None):
        """给模型看过了 = 用到了（不淡出）"""
        ids = {str(i) for i in ids or ()}
        if not ids:
            return
        now = float(now if now is not None else self.clock())
        hit = False
        for x in self.lessons:
            if str(x.get("id")) in ids:
                x["used"] = int(x.get("used") or 0) + 1
                x["used_t"] = now
                hit = True
        if hit:
            self.save()

    # ───────── 管 ─────────
    def get(self, lid):
        return next((x for x in self.lessons if str(x.get("id")) == str(lid)), None)

    def delete(self, lid):
        n0 = len(self.lessons)
        self.lessons = [x for x in self.lessons if str(x.get("id")) != str(lid)]
        if len(self.lessons) != n0:
            self.save()
            return True
        return False

    def edit(self, lid, fields):
        """改一条（什么情况 / 错的说法 / 对的说法 / 依据 / 话题词）→ True / False；话题词跟着重算"""
        f = {
            k: short(v, LIMITS[k])
            for k, v in (fields or {}).items()
            if k in EDITABLE and v is not None
        }
        x = self.get(lid)
        if not f or x is None:
            return False
        x.update(f)
        if x.get("kind", "事实") == "事实":
            x["keys"] = sorted(
                topic_keys(
                    x.get("topic"), x.get("wrong"), x.get("right"), x.get("when")
                )
            )[:40]
        x["edited"] = round(self.clock())
        self.save()
        return True

    def wipe_user(self, uid, names=()):
        """删数据：他指出的、关于他这个号的、提到他名字 / QQ 号的教训整条删 → 删了几条
        v0.3.1（11-10 审查 VP1-8）：按名字找只看事实教训的内容（什么情况 / 错的 / 对的 / 依据 / 话题词）——
          「X指出、核对过」「回「X」（验证程序）」这种固定格式的字不看（他指出的、回他那个号的，上面按 QQ 号已经删了）；
          以前名片叫「核对过」的人一删数据，所有事实教训都没了。names 传空 = 只按 QQ 号删（自助的「忘记我」）"""
        uid = str(uid)
        names = [str(n) for n in (names or ()) if n and len(str(n)) >= 2]

        def about(x):
            if str(x.get("uid")) == uid or uid in set(x.get("uids") or []):
                return True
            if x.get("private") and str(x.get("conv") or "").endswith(":" + uid):
                return True
            blob = " ".join(
                str(x.get(k) or "")
                for k in ("when", "wrong", "right", "basis", "topic", "src", "name")
            )
            if len(uid) >= 5 and re.search(
                r"(?<!\d)" + re.escape(uid) + r"(?!\d)", blob
            ):
                return True
            if x.get("kind", "事实") != "事实":
                return False
            free = " ".join(
                str(x.get(k) or "") for k in ("when", "wrong", "right", "basis", "topic")
            )
            return any(n in free for n in names)

        n0 = len(self.lessons)
        self.lessons = [x for x in self.lessons if not about(x)]
        n = n0 - len(self.lessons)
        if n:
            self.save()
        return n

    def view(self, now=None):
        """面板：教训（新的在前；淡出了的标出来）"""
        now = float(now if now is not None else self.clock())
        rows = []
        for x in sorted(
            self.lessons, key=lambda x: -float(x.get("last") or x.get("t") or 0)
        ):
            r = {
                k: x.get(k)
                for k in (
                    "id",
                    "kind",
                    "t",
                    "last",
                    "n",
                    "used",
                    "used_t",
                    "when",
                    "wrong",
                    "right",
                    "basis",
                    "topic",
                    "src",
                    "name",
                    "edited",
                    "private",
                )
            }
            r["faded"] = self.faded(x, now)
            r["weight"] = round(self.weight(x, now), 2)
            rows.append(r)
        return {
            "lessons": rows,
            "lesson_days": float(self.cfg["lesson_days"]),
            "half_days": float(self.cfg["half_days"]),
            "lesson_max": int(self.cfg["lesson_max"]),
        }

    def counts(self, now=None):
        now = float(now if now is not None else self.clock())
        live = [x for x in self.lessons if not self.faded(x, now)]
        return {
            "all": len(self.lessons),
            "live": len(live),
            "fact": sum(1 for x in self.lessons if x.get("kind", "事实") == "事实"),
            "format": sum(1 for x in self.lessons if x.get("kind") == "格式"),
        }
