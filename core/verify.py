# -*- coding: utf-8 -*-
"""
入口 2：回答没过格式 / 验证要求（不依赖 AstrBot，可以单独测）

起因（作者自己的机器人碰到过）：被拉进一个群，入群验证机器人出算术题（「请在 60 秒内发送 X 的运算结果 否则移出群聊」）。
它每次都答对了数字，但后面总要多加一句，连着被判失败、被移出群。每次失败它都看到了，却一直以为是对方坏了。

这里只管「认出来 + 当场给状态 + 发之前查格式 + 止损 + 记教训」，不替它答题（不算任何算式）。
只认「机器 / 验证程序」那种场合：对方是别人家的机器人 / 它刚回的那句就是验证题 / 这句本身是验证程序的口气
（验证 / 否则移出 / 超时踢出）/ 以前在这个号这儿吃过亏。真人跟它玩猜数字说「答案错误」、管理员说的，一律不算。
  1 认出「没过」（observe）：冲它说的、它刚回完他 gap_sec 秒内：「验证失败 / 答案错误 / 格式不对 / 请重新发送 / 你还有 N 次机会」
      → 一次没过（强）；同一道题原样又发了一遍（数字一样、字基本一样）→ 一次弱的（两次弱的才算进纠错）
  2 纠错这一轮（turn）：没过过一次（弱的两次）、他又在判 / 又出了题 → 给模型一段状态：最近几次它说了什么、对方回了什么、
      这条要的格式。只写事实，不写它该怎么说
  3 发之前查（check）—— 只管一种：只发数字。验证题（一句话里有算式 + 要结果 + 硬要求）、对方是机器人又明说只要数字 / 出了算式要结果
      → 先从它自己的话里取（「26 嘛」→「26」、「答案是 26，不是 62」→「26」）；取不出来 / 负数 / 只是抄了题里的数 → 外壳让模型重写一次
      → 还不行这条就不发
  4 止损：进了纠错还连着没过 stop_after 次 → 对这个人停 stop_min 分钟
  5 教训：纠错以后过了 / 停了 → 记一条「回这个号：他要的是只发一个数字」（core/lessons.py）；以后再碰到同一个号出题 / 判它时带上
"""

import re
import time
import unicodedata
from difflib import SequenceMatcher

from .text import short

DEFAULTS = {
    "window_min": 15.0,  # 多少分钟内的几次没过算「连着」
    "gap_sec": 120.0,  # 它回完多少秒内对方判它，才算在判它那句
    "stop_after": 2,  # 进了纠错以后再没过几次就停
    "stop_min": 30.0,  # 停多少分钟
}

FAIL = re.compile(
    r"验证(?:失败|未通过|不通过|错误)|未通过验证|答案(?:错误|不对|不正确|有误)|回答(?:错误|不对|不正确|有误)|"
    r"格式(?:错误|不对|不正确|有误)|不符合(?:要求|格式|规则)|无效的?(?:回答|答案|输入|格式|回复)|输入(?:有误|错误|无效)|"
    r"请重新(?:发送|回答|回复|输入|作答)|请发送正确|你还(?:有|剩)[\s「\[【(（]{0,6}\d+[\s」\]】)）]{0,6}次|"
    r"剩余[\s「\[【(（]{0,6}\d+[\s」\]】)）]{0,6}次|\bwrong answer\b|\bincorrect\b|\btry again\b",
    re.I,
)
PASS = re.compile(
    r"验证(?:成功|通过)|通过(?:了)?验证|回答正确|答案正确|答对了|✅|恭喜.{0,8}(?:通过|答对)",
    re.I,
)
# 验证程序的口气（硬要求：答不对有后果 / 限时 / 验证 / 还剩几次）
_HARD = re.compile(
    r"否则.{0,12}(?:移出|踢|撤回|禁言|清退|拒绝|失败|判)|超时.{0,8}(?:移出|踢|清退|失败|判)|限时|"
    r"\d+[\s」\]】)）]{0,6}(?:秒|分钟|s|min)\s{0,3}之?内|验证|你还(?:有|剩).{0,4}\d+.{0,4}次|剩余.{0,4}\d+.{0,4}次",
    re.I,
)
# 光凭这句话本身认「是验证程序在判它」：只认「验证 / 否则移出 / 超时踢出」（「你还有 4 次机会」真人玩猜数字也这么说）
_VERIFY = re.compile(
    r"验证|否则.{0,12}(?:移出|踢|清退|禁言)|超时.{0,8}(?:移出|踢|清退)"
)
_ONLY_DIGITS = re.compile(
    r"(?:只|仅)(?:能|许|准|需|要|可以)?(?:回复|回答|发送|发|输入|写|说|给出|填)?(?:一个)?(?:纯)?(?:阿拉伯)?数字|"
    r"数字(?:即可|就行|就好|就可以)|"
    r"only (?:reply|answer|send|type)(?: with)? (?:a |the )?(?:number|digits?)",
    re.I,
)
_REQ = re.compile(
    r"(?:运算|计算)(?:的)?结果|得数|等于(?:多少|几)|结果(?:是|为)?(?:多少|几)|答案(?:是|为)?(?:多少|几)|"
    r"\d\s*[=＝]\s*(?:[?？]|$)|的结果|的(?:答案|得数)|(?:计算|算出|算一下)[^。\n]{0,24}(?:发送|回复|答案|结果|输入)|"
    r"(?:回复|发送|输入|给出|回答)[^。\n]{0,16}答案|"
    r"(?:回复|回答|发送|输入|填写?|给出)(?:一个)?(?:纯)?(?:阿拉伯)?数字(?!的)|"
    + _ONLY_DIGITS.pattern,
    re.I | re.M,
)
_EXPR = re.compile(r"\d+(?:\.\d+)?\s*[-+*/×÷xX＋－＊／]\s*\d+")
_AT = re.compile(r"@\S+\s*")
_NUM = re.compile(r"(?<![\d.])[-+]?\d+(?:\.\d+)?(?![\d.])")
_EQ_NUM = re.compile(
    r"(?:=|(?<!不)等于|答案(?:是|为|:)|结果(?:是|为|:)|得数(?:是|为|:)?)\s*([-+]?\d+(?:\.\d+)?)(?![\d.])"
)
_LEAD = re.compile(
    r"^\s*([-+]?\d+(?:\.\d+)?)(?![\d.])"
    r"(?!\s*(?:[-+*/×÷xX^=,、~]|和|与|跟|加|减|乘|除|到|至)\s*\d)"
    r"(?!\s*(?:秒|分|次|个|岁|块|元|%|号|点|年|月|日|天|小时|遍|人|条|张|s\b|min\b))",
    re.I,
)
DIGITS = {"kind": "digits", "desc": "只发一个数字"}
CAP = 800
HEAD = "\n# 格式要求（系统看出来的 不是谁说的话）\n"


def _nfkc(t):
    return (
        unicodedata.normalize("NFKC", str(t or "")[:CAP])
        .replace("−", "-")
        .replace("－", "-")
    )


def _own(t):
    """一句话本身（@ 去掉）"""
    return _nfkc(_AT.sub(" ", str(t or "")[: CAP + 300]))


def _join(texts):
    return " ".join(_own(x) for x in (texts or []) if x)


def machine_prompt(text):
    """验证程序那种题（一句话里同时有）：一个算式 + 要结果 + 硬要求。几句拼起来凑出来的不算"""
    t = _own(text)
    return bool(_EXPR.search(t)) and bool(_REQ.search(t)) and bool(_HARD.search(t))


def want_digits(text):
    """明说只要数字 / 出了算式要结果（一句话里）"""
    t = _own(text)
    return bool(_ONLY_DIGITS.search(t)) or (
        bool(_EXPR.search(t)) and bool(_REQ.search(t))
    )


def constraint(texts, machine=False):
    """他这几句里要的格式 → {"kind": "digits", "desc", "nums"}；没有 → None
    验证题（machine_prompt）谁发的都算；machine（对方是机器人 / 以前在他这儿吃过亏）时明说只要数字、出了算式要结果也算。
    nums：题里出现的数（它只是把题里的数抄了一个 → 不算它给的答案）"""
    ts = [x for x in (texts or []) if x and str(x).strip()]
    if not ts:
        return None
    if any(machine_prompt(x) for x in ts) or (
        machine and any(want_digits(x) for x in ts)
    ):
        return dict(DIGITS, nums=list(dict.fromkeys(_NUM.findall(_join(ts))))[:12])
    return None


def _num(s):
    try:
        f = float(s)
        return int(f) if f == int(f) else f
    except (TypeError, ValueError, OverflowError):
        return s


def _neg_before(s, i):
    """数前面是「负」/ 隔着空格的「-」：它说的是负数 —— 不能只取后面的数（「91 - 65」那种算式不算）"""
    j = i
    while j > 0 and s[j - 1] in " \t":
        j -= 1
    if j > 0 and s[j - 1] == "负":
        return True
    if j > 0 and s[j - 1] == "-" and j < i:
        k = j - 1
        while k > 0 and s[k - 1] in " \t":
            k -= 1
        return not (k > 0 and s[k - 1].isdigit())
    return False


def check(con, text):
    """这条合不合「只发一个数字」→ (合格, 要发的)：
    合格 → (True, 原样)；不合格但能从它自己写的里面取出来 → (False, 取出来的)；取不出来 / 分不清 → (False, None)
    取的顺序：①「= / 等于 / 答案是 / 结果是 / 得数是」后面那个（几个不一样 → 分不清）②开头就是的那个数
    ③整句只有一个数 ④都不是 → None。只从它写的里面取、不替它算；它只是抄了题里的数（②③）不算"""
    t = str(text or "").strip()
    if not t or not con or con.get("kind") != "digits":
        return False, None
    s = _nfkc(t).strip()
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", s):
        return True, s
    eq = list(dict.fromkeys(_num(x) for x in _EQ_NUM.findall(s)))
    if len(eq) == 1:
        return False, str(eq[0])
    if len(eq) > 1:
        return False, None
    avoid = {_num(x) for x in (con.get("nums") or [])}
    m = _LEAD.match(s)
    if m and _num(m.group(1)) not in avoid:
        return False, m.group(1)
    nums = list(_NUM.finditer(s))
    if (
        len(nums) == 1
        and _num(nums[0].group()) not in avoid
        and not _neg_before(s, nums[0].start())
    ):
        return False, nums[0].group()
    return False, None


def rewrite_prompt(con, question, text):
    """重写那一次给模型的话（题目原文 + 它刚才写的 + 要求）"""
    return (
        f"对方发来的题目：「{short(question, 300)}」\n"
        f"你刚才回的：「{short(text, 80)}」\n"
        f"这条要{(con or {}).get('desc') or '只发一个数字'}：刚才那条多了别的字，或者看不出哪个是答案。"
        "重写这一条：只输出那个数字本身（阿拉伯数字），别的一个字都不要。"
    )


def _skel(t):
    """「同一个要求又发了一遍」：@ 不算、标点空格不算 —— 数字要一样（换了数字是新的一题）"""
    t = _AT.sub("", _nfkc(t))
    return re.sub(r"[\s，。！？!?、,.:：;；「」【】\[\]()（）\"'“”~～…·-]+", "", t)


def similar(a, b):
    x, y = _skel(str(a or "")[:300]), _skel(str(b or "")[:300])
    if len(x) < 6 or len(y) < 6:
        return False
    if re.findall(r"\d+", x) != re.findall(r"\d+", y):
        return False
    return x == y or SequenceMatcher(None, x, y).ratio() >= 0.9


class Verify:
    """一个插件一个。这一阵的状态只在内存里（重启就清 —— 本来就是几分钟里的事）；教训交给 LessonStore"""

    _JUDGED = ("判没过", "又发了一遍同样的要求")

    def __init__(self, cfg=None, lessons=None, clock=time.time):
        self.cfg = dict(DEFAULTS)
        self.set_cfg(cfg or {})
        self.lessons = lessons
        self.clock = clock
        self.last = {}  # (对话, 对方 QQ) → 它最近一次回他：{"t", "text", "prompt", "con", "name", "machine"}
        self.st = {}  # (对话, 对方 QQ) → {"name", "fails": [...], "mode_t", "since_mode", "stop_until", "con"}
        self.events = []  # 最近发生的事（面板）

    def set_cfg(self, cfg):
        for k, v in (cfg or {}).items():
            if k in DEFAULTS and v is not None:
                try:
                    self.cfg[k] = type(DEFAULTS[k])(v)
                except (TypeError, ValueError):
                    pass

    def _now(self, now):
        return float(now if now is not None else self.clock())

    def _win(self):
        return float(self.cfg["window_min"]) * 60

    def _recent(self, st, now):
        return [f for f in (st or {}).get("fails") or [] if now - f["t"] <= self._win()]

    def _mode(self, st, now):
        f = self._recent(st, now)
        return sum(1 for x in f if x["strong"]) >= 1 or len(f) >= 2

    def _judged(self, st, now):
        return any(x["kind"] in self._JUDGED for x in self._recent(st, now))

    def _format_lessons(self, uid, now):
        if self.lessons is None:
            return []
        try:
            return self.lessons.for_uid(uid, now)
        except Exception:
            return []

    def _digits_lesson(self, uid, now):
        return any(
            (x.get("con") or {}).get("kind") == "digits"
            for x in self._format_lessons(uid, now)
        )

    def _event(self, conv, uid, name, kind, text, now):
        self.events.append(
            {
                "t": now,
                "conv": conv,
                "uid": str(uid),
                "name": short(name, 20),
                "kind": kind,
                "text": short(text, 100),
            }
        )
        del self.events[:-40]

    def _lesson(self, conv, uid, name, con, src, now):
        if self.lessons is None:
            return None
        try:
            return self.lessons.add_format(conv, uid, name, con, src, now)
        except Exception:
            return None

    # ───────── 它回完一个人 ─────────
    def note_reply(
        self, conv, uid, name, text, prompt="", con=None, machine=False, now=None
    ):
        k = (str(conv), str(uid))
        now = self._now(now)
        machine = bool(machine) or bool(con) or machine_prompt(prompt)
        self.last[k] = {
            "t": now,
            "text": short(text, 200),
            "prompt": short(prompt, 300),
            "con": con,
            "name": name or "",
            "machine": machine,
        }
        if len(self.last) > 2000:
            for x in sorted(self.last, key=lambda x: self.last[x]["t"])[:500]:
                self.last.pop(x, None)

    def stopped(self, conv, uid, now=None):
        st = self.st.get((str(conv), str(uid)))
        return bool(st) and float(st.get("stop_until") or 0) > self._now(now)

    def stop_left(self, conv, uid, now=None):
        st = self.st.get((str(conv), str(uid)))
        return max(0, int(float((st or {}).get("stop_until") or 0) - self._now(now)))

    def _prune(self, keep, now):
        if len(self.st) <= 500:
            return
        for x in [
            x
            for x, s in self.st.items()
            if x != keep
            and not self._recent(s, now)
            and float(s.get("stop_until") or 0) <= now
        ]:
            self.st.pop(x, None)

    def _fail(self, conv, uid, name, her, fb, kind, strong, now):
        k = (str(conv), str(uid))
        st = self.st.get(k)
        if st is None:
            st = self.st[k] = {
                "name": name or "",
                "fails": [],
                "mode_t": 0.0,
                "since_mode": 0,
                "stop_until": 0.0,
                "con": (self.last.get(k) or {}).get("con"),
            }
            self._prune(k, now)
        if name:
            st["name"] = name
        was = self._mode(st, now)
        fb = re.sub(r"\s*\n\s*", " / ", str(fb or "")).strip(" /")
        st["fails"] = self._recent(st, now)[-9:] + [
            {
                "t": now,
                "her": short(her, 120),
                "fb": short(fb, 160),
                "kind": kind,
                "strong": bool(strong),
            }
        ]
        ev = {
            "kind": "fail",
            "conv": k[0],
            "uid": k[1],
            "name": st["name"],
            "why": kind,
            "fb": short(fb, 80),
            "n": len(st["fails"]),
            "mode": self._mode(st, now),
        }
        if ev["mode"] and not was:  # 刚进纠错：下一轮给状态
            st["mode_t"], st["since_mode"] = now, 0
            ev["kind"] = "mode"
        elif was:  # 纠错以后还在没过：够 stop_after 次就停（止损）
            st["since_mode"] = int(st.get("since_mode") or 0) + 1
            if (
                st["since_mode"] >= int(self.cfg["stop_after"])
                and float(st.get("stop_until") or 0) <= now
            ):
                st["stop_until"] = now + float(self.cfg["stop_min"]) * 60
                ev["kind"] = "stop"
                if self._judged(st, now):
                    ev["lesson"] = self._lesson(
                        k[0],
                        k[1],
                        st["name"],
                        st.get("con") or (self.last.get(k) or {}).get("con"),
                        "stop",
                        now,
                    )
        self._event(k[0], k[1], st["name"], ev["kind"], f"{kind}：{short(fb, 60)}", now)
        return ev

    def observe(
        self, conv, uid, name, text, now=None, to_her=False, isbot=False, admin=False
    ):
        """收到一句话：是不是在判它刚才那句没过 / 说它过了 → 事件 dict；跟它无关 → None
        要同时满足：冲它说的、它刚回完他（gap_sec 内）、不是管理员、是机器那种场合"""
        if admin or not to_her:
            return None
        now = self._now(now)
        k = (str(conv), str(uid))
        last = self.last.get(k)
        if not last or not 0 <= now - last["t"] <= float(self.cfg["gap_sec"]):
            return None
        t = _own(text)
        if not (
            isbot
            or last.get("machine")
            or _VERIFY.search(t)
            or self._digits_lesson(uid, now)
        ):
            return None
        st = self.st.get(k)
        if PASS.search(t) and not FAIL.search(t):
            if st and self._recent(
                st, now
            ):  # 纠错以后过了：记教训（下次直接按这个格式）
                con = st.get("con") or last.get("con")
                lid = self._lesson(k[0], k[1], st.get("name") or name, con, "pass", now)
                self._event(k[0], k[1], st.get("name") or name, "pass", t[:60], now)
                self.st.pop(k, None)
                return {
                    "kind": "pass",
                    "conv": k[0],
                    "uid": k[1],
                    "name": name,
                    "con": con,
                    "lesson": lid,
                }
            return None
        if FAIL.search(t):
            return self._fail(
                k[0], k[1], name, last.get("text"), t.strip(), "判没过", True, now
            )
        if last.get("prompt") and similar(t, last["prompt"]):
            return self._fail(
                k[0],
                k[1],
                name,
                last.get("text"),
                t.strip(),
                "又发了一遍同样的要求",
                False,
                now,
            )
        return None

    # ───────── 这一轮 ─────────
    def turn(self, conv, uid, name, texts, now=None, isbot=False, admin=False):
        """这一轮回他之前：要不要给格式状态 / 要不要只发数字 → {"active", "mode", "con", "block", "lessons"}
        active（这一轮要管）：要只发数字 / 在纠错里、他又在判它或又出了题。纠错里随便聊的、真人随口问算术：都不管"""
        now = self._now(now)
        k = (str(conv), str(uid))
        st = None if admin else self.st.get(k)
        mode = bool(st) and self._mode(st, now)
        fail_now = any(FAIL.search(_own(x)) for x in (texts or []) if x)
        mp = any(machine_prompt(x) for x in (texts or []) if x)
        ls = [] if admin else self._format_lessons(uid, now)
        machine = not admin and (
            bool(isbot) or any((x.get("con") or {}).get("kind") == "digits" for x in ls)
        )
        con = constraint(texts, machine=machine)
        if (
            con is None and mode and fail_now
        ):  # 他又在判它（这句没再写题）：还按刚才那道题的格式
            con = st.get("con") or (self.last.get(k) or {}).get("con")
        if st is not None and con:
            st["con"] = con
        active = con is not None or (mode and (fail_now or mp))
        out = {
            "active": active,
            "mode": mode,
            "con": con,
            "machine": bool(machine or mp or active),
            "block": "",
            "lessons": [],
        }
        if not active:
            return out
        who = name or "他"
        lines = []
        if con:
            lines.append(
                f"{who}这条要的是{con.get('desc') or '只发一个数字'}（看起来是验证程序出的题，按格式判，多了别的字算没过）。"
            )
        if mode:
            f = self._recent(st, now)
            lines.append(f"你回{who}的话最近 {len(f)} 次没过：")
            for x in f[-4:]:
                lines.append(
                    f"- 你说「{short(x['her'], 40)}」→ "
                    + (f"他回「{short(x['fb'], 70)}」" if x["fb"] else x["kind"])
                )
            if not con:
                lines.append("系统没看出他具体要什么格式；他判你没过的原话在上面。")
        if con:
            lines.append(
                "系统发出去之前会检查：不是只有一个数字的，只留那个数字；取不出来就重写一次，还不行这条不发。"
            )
        out["block"] = HEAD + "\n".join(lines) + "\n"
        if ls and self.lessons is not None:
            out["lessons"] = [x.get("id") for x in ls[:3]]
            out["block"] += self.lessons.format_text(ls[:3])
        return out

    # ───────── 管 ─────────
    def clear(self, conv, uid):
        """面板「解除」：这个人的纠错 / 停着的都放开"""
        return self.st.pop((str(conv), str(uid)), None) is not None

    def forget_uid(self, uid, names=()):
        """删数据：他的这一阵状态、它回他的记录、提到他的事件"""
        uid = str(uid)
        names = [str(n) for n in (names or ()) if n and len(str(n)) >= 2]
        n = 0
        for d in (self.st, self.last):
            for k in [k for k in d if k[1] == uid]:
                d.pop(k, None)
                n += 1
        old = len(self.events)
        self.events = [
            e
            for e in self.events
            if e.get("uid") != uid
            and not any(x in (e.get("name", "") + e.get("text", "")) for x in names)
        ]
        return n + old - len(self.events)

    def live(self, now=None):
        """面板：这一阵在纠错 / 停着的"""
        now = self._now(now)
        out = []
        for (conv, uid), st in self.st.items():
            f = self._recent(st, now)
            if not f and float(st.get("stop_until") or 0) <= now:
                continue
            out.append(
                {
                    "conv": conv,
                    "uid": uid,
                    "name": st.get("name") or "",
                    "fails": len(f),
                    "mode": self._mode(st, now),
                    "stop_left": max(0, int(float(st.get("stop_until") or 0) - now)),
                    "last": short(f[-1]["fb"], 60) if f else "",
                    "con": (st.get("con") or {}).get("desc", ""),
                }
            )
        return out
