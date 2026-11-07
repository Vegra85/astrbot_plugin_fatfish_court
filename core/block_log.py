# -*- coding: utf-8 -*-
"""
拉黑庭的卷宗材料（不依赖 AstrBot，可以单独测）：记每个人最近说的话（带标记），开庭时拼卷宗用。

AstrBot 不留群聊的逐条存档，所以这里自己记一份，只记拼卷宗要的、尽量少：
  - 每个人最近 window_days 天里的话：冲机器人说的（[本人→机器人]）最多 per_user 条，别的（没冲它 / 引用别人 / 转发）最多 10 条；
    每条 ≤120 字，只记他自己打的字（引用的那段去掉、转发的不展开）
  - 每个对话最近 20 行（「名字: 话」，机器人说的记「机器人」）：【刚才那段】
  - 他用过的名片（最多 8 个）、第一次见到的时间
  - v0.2.0：机器人回他的话（私聊里的、群里回他那句的）最多 bot_per_user 条、每条 ≤120 字 —— 卷宗里穿插着给法官看（不编号、不当证据）
最多记 max_users（500）个人（最久没说话的先扔）。存在 data/plugin_data/astrbot_plugin_fatfish_court/msgs.json，删数据会删掉。
"""

import json
import os
import re
import time
from collections import deque
from pathlib import Path

from .block_judge import TAG_FWD, TAG_NOT, TAG_OWN, TAG_QUOTE

DEFAULTS = {"window_days": 7, "per_user": 40, "other_per_user": 10, "bot_per_user": 60, "max_users": 500}
CTX_LINES = 20


def _clip(t, n):
    return re.sub(r"\s+", " ", str(t or "")).strip()[:n]


def tag_of(
    private=False,
    at_self=False,
    at_other=False,
    reply_self=False,
    reply_other=False,
    forward=False,
    woke=False,
    named=False,
    text="",
    names=(),
):
    """他这句话的标记（照作者自己的机器人 v1.17.13 的标法）：
    私聊：转发 / 本人→机器人；群里：转发 > 引用别人 > @它 / 引用它（本人→机器人）> @了别人（本人·没冲机器人）>
    唤醒词叫它、或者提到它的名字而且像在叫它（带「你」/ 名字在开头或结尾）→ 本人→机器人；别的 → 本人·没冲机器人"""
    if private:
        return TAG_FWD if forward else TAG_OWN
    if forward:
        return TAG_FWD
    if reply_other and not reply_self:
        return TAG_QUOTE
    if at_self or reply_self:
        return TAG_OWN
    if at_other:
        return TAG_NOT
    if woke:
        return TAG_OWN
    if named:
        core = re.sub(r"[\s，,。.!！?？~～…]+", "", str(text or ""))
        if "你" in str(text or "") or any(
            n and (core.startswith(n) or core.endswith(n)) for n in names or ()
        ):
            return TAG_OWN
    return TAG_NOT


def to_bot(tag):
    return tag == TAG_OWN


def where_map():
    """拼卷宗时把对话写成「私聊 / 群 A / 群 B」（群号不进卷宗）→ 函数 where(conv)"""
    code = {}

    def where(conv):
        c = str(conv or "")
        if ":FriendMessage:" in c:
            return "私聊"
        if c not in code:
            k = len(code)
            code[c] = (
                "群 "
                + "ABCDEFGHIJKLMNOPQRSTUVWXYZ"[k % 26]
                + (str(k // 26 + 1) if k >= 26 else "")
            )
        return code[c]

    return where


class MsgLog:
    def __init__(self, path, cfg=None, clock=time.time, warn=None):
        self.path = Path(path)
        self.cfg = dict(DEFAULTS)
        self.set_cfg(cfg or {})
        self.clock = clock
        self.warn = warn or (lambda m: None)
        self.users = {}  # QQ → {"own": [...], "other": [...], "names": [...], "first": t, "last": t, "conv": 最近说话的对话}
        self.ctx = {}  # 对话 → deque[(谁, 话, QQ)]（不存盘：重启就清，本来就是「刚才那段」）
        self.dirty = False
        self.load()

    def set_cfg(self, cfg):
        for k, v in (cfg or {}).items():
            if k in DEFAULTS and v is not None:
                try:
                    self.cfg[k] = int(float(v))
                except (TypeError, ValueError):
                    pass

    # ───────── 存档 ─────────
    def load(self):
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            self.warn(f"拉黑庭记的聊天读不了（当成空的，原文件没动）：{e!r}"[:200])
            return
        us = d.get("users") if isinstance(d, dict) else None
        if isinstance(us, dict):
            self.users = {str(k): v for k, v in us.items() if isinstance(v, dict)}

    def save(self, force=False):
        if not (self.dirty or force):
            return True
        try:
            self._expire(self.clock())
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"users": self.users}, ensure_ascii=False), encoding="utf-8"
            )
            os.replace(tmp, self.path)
            self.dirty = False
            return True
        except OSError as e:
            self.warn(f"拉黑庭记的聊天写不进去：{e!r}"[:200])
            return False

    def _expire(self, now):
        lim = now - float(self.cfg["window_days"]) * 86400
        for uid in list(self.users):
            u = self.users[uid]
            for k in ("own", "other", "bot"):
                u[k] = [
                    m
                    for m in u.get(k) or []
                    if isinstance(m, dict) and float(m.get("t") or 0) >= lim
                ]
            if (
                not u.get("own")
                and not u.get("other")
                and float(u.get("last") or 0) < lim
            ):
                self.users.pop(uid, None)
        mx = max(10, int(self.cfg["max_users"]))
        if len(self.users) > mx:
            for uid in sorted(
                self.users, key=lambda k: float(self.users[k].get("last") or 0)
            )[: len(self.users) - mx]:
                self.users.pop(uid, None)

    # ───────── 记 ─────────
    def note(self, uid, name, conv, text, tag, mid=None, now=None):
        """他说了一句（已经算好标记）"""
        uid = str(uid or "")
        text = _clip(text, 120)
        if not uid or not text:
            return
        now = float(now if now is not None else self.clock())
        u = self.users.setdefault(
            uid, {"own": [], "other": [], "names": [], "first": int(now)}
        )
        rec = {"t": round(now, 3), "conv": str(conv or ""), "tag": tag, "text": text}
        if mid is not None:
            rec["mid"] = str(mid)
        k = "own" if tag == TAG_OWN else "other"
        lim = (
            int(self.cfg["per_user"]) if k == "own" else int(self.cfg["other_per_user"])
        )
        u[k] = (list(u.get(k) or []) + [rec])[-max(1, lim) :]
        nm = _clip(name, 20)
        if nm and nm != uid:
            ns = [x for x in u.get("names") or [] if x != nm] + [nm]
            u["names"] = ns[-8:]
        u["last"] = int(now)
        u["conv"] = str(conv or "")
        self.dirty = True
        if len(self.users) > int(self.cfg["max_users"]) + 50:
            self._expire(now)

    def note_ctx(self, conv, who, text, uid=""):
        q = self.ctx.get(conv)
        if q is None:
            q = self.ctx[conv] = deque(maxlen=CTX_LINES)
            if len(self.ctx) > 600:
                for k in list(self.ctx)[:200]:
                    self.ctx.pop(k, None)
        t = _clip(text, 120)
        if t:
            q.append((_clip(who, 16), t, str(uid or "")))

    def note_bot(self, uid, conv, text, now=None):
        """机器人回了他一句（私聊里的、群里回他那句的；v0.2.0）—— 他没说过话的不记"""
        uid = str(uid or "")
        text = _clip(text, 120)
        u = self.users.get(uid)
        if not uid or not text or not isinstance(u, dict):
            return
        now = float(now if now is not None else self.clock())
        lim = max(1, int(self.cfg["bot_per_user"]))
        u["bot"] = (list(u.get("bot") or []) + [{"t": round(now, 3), "conv": str(conv or ""), "text": text}])[-lim:]
        self.dirty = True

    # ───────── 拼卷宗要的 ─────────
    def bot_for(self, uid, since=0.0, now=None):
        """机器人 window_days 天里（since 之后）回他的话，按时间 → [{t, conv, text}]（v0.2.0）"""
        now = float(now if now is not None else self.clock())
        lim = max(now - float(self.cfg["window_days"]) * 86400, float(since or 0))
        u = self.users.get(str(uid)) or {}
        return sorted(
            [dict(b) for b in u.get("bot") or [] if isinstance(b, dict) and lim <= float(b.get("t") or 0) <= now + 60],
            key=lambda b: float(b.get("t") or 0),
        )

    def msgs_for(self, uid, since=0.0, now=None):
        """他 window_days 天里（since 之后）的话，按时间 → [{t, mid, uid, conv, tag, text}]"""
        now = float(now if now is not None else self.clock())
        lim = max(now - float(self.cfg["window_days"]) * 86400, float(since or 0))
        u = self.users.get(str(uid)) or {}
        out = [
            dict(m, uid=str(uid))
            for m in (u.get("own") or []) + (u.get("other") or [])
            if isinstance(m, dict) and lim <= float(m.get("t") or 0) <= now + 60
        ]
        return sorted(out, key=lambda m: float(m.get("t") or 0))

    def ctx_for(self, conv, after=0.0):
        return [(w, t) for w, t, _u in list(self.ctx.get(conv) or [])]

    def names_of(self, uid):
        return list((self.users.get(str(uid)) or {}).get("names") or [])

    def first_seen(self, uid):
        return (self.users.get(str(uid)) or {}).get("first")

    def last_conv(self, uid):
        return (self.users.get(str(uid)) or {}).get("conv") or ""

    def spoke_to_bot(self, uid, conv, within_sec, now=None):
        """他 within_sec 秒内在这个对话里冲机器人说过话吗"""
        now = float(now if now is not None else self.clock())
        u = self.users.get(str(uid)) or {}
        return any(
            str(m.get("conv")) == str(conv)
            and now - float(m.get("t") or 0) <= within_sec
            for m in u.get("own") or []
            if isinstance(m, dict)
        )

    def find(self, conv, key, within_sec=1800, now=None):
        """模型写的「对谁」（QQ 号 / 名字）→ 这个对话里 within_sec 秒内说过话的那个人的 QQ；认不出 → \"\""""
        now = float(now if now is not None else self.clock())
        key = str(key or "").strip().lstrip("@")
        if not key:
            return ""
        digits = re.sub(r"\D", "", key)
        for uid, u in self.users.items():
            if digits and uid == digits:
                return uid
        hits = []
        for uid, u in self.users.items():
            if (
                str(u.get("conv")) != str(conv)
                or now - float(u.get("last") or 0) > within_sec
            ):
                continue
            if any(
                key == n or (len(key) >= 2 and key in n) for n in u.get("names") or []
            ):
                hits.append(uid)
        return hits[0] if len(hits) == 1 else ""

    # ───────── 删数据 ─────────
    def forget(self, uid, names=()):
        uid = str(uid or "")
        n = 0
        u = self.users.pop(uid, None)
        if u:
            n += len(u.get("own") or []) + len(u.get("other") or []) + len(u.get("bot") or [])
            names = set(names or ()) | set(u.get("names") or [])
        names = {x for x in names if x and len(str(x)) >= 2}
        for conv in list(self.ctx):
            q = self.ctx[conv]
            if conv.endswith(":" + uid) and ":FriendMessage:" in conv:
                n += len(q)
                self.ctx.pop(conv, None)
                continue
            keep = [x for x in q if x[2] != uid and x[0] not in names]
            if len(keep) != len(q):
                n += len(q) - len(keep)
                self.ctx[conv] = deque(keep, maxlen=CTX_LINES)
        self.dirty = True
        self.save(force=True)
        return n

    def count(self):
        return {
            "users": len(self.users),
            "msgs": sum(
                len(u.get("own") or []) + len(u.get("other") or [])
                for u in self.users.values()
            ),
        }


__all__ = [
    "MsgLog",
    "tag_of",
    "to_bot",
    "where_map",
    "TAG_OWN",
    "TAG_NOT",
    "TAG_QUOTE",
    "TAG_FWD",
]
