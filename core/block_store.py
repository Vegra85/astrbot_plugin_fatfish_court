# -*- coding: utf-8 -*-
"""
拉黑庭的名单和记账（不依赖 AstrBot，可以单独测）：一个文件 block.json，改一次写一次（原子替换）。

  blocked  {QQ: {t, name, why, conv, cites, case, by}}     永久不理的人（删除判下来的）
  cool     {QQ: {t, until, why, conv, name, case, by}}     冷却中的人（立案的占位也记在这：until = 占位到几点）
  cases    [案子]                                         审理中 / 冷却中 / 结案；冷却中的案子下面收证据 ev
  cooled   {QQ: {t 第一次, last 最近一次冷却到几点, case, n}}  冷却过的人（冷却到点才算；不按 keep_days 过期）
  pardons  {QQ: 撤销的时间}                                管理员撤销（之前的线索、案子不再算）
  marks    {QQ: [线索]}                                   代码抓的线索（冒充管理员）：只给法官当线索，不算任何门槛
  deletes  [删除的记录]                                    判删除执行的每一次（删好友 / 清数据 本来会 / 真做了）—— 每天的上限数它
  tries    [立案的记录]                                    每次立案（不管立没立成）
  judged   {QQ: {n, last}}                                被判过几次（冷却 / 删除；AstrBot 没有好感度，只记次数）
  appeals  {QQ: [申诉]}                                   申诉（转给管理员的）
  notes    {QQ: [给模型的一行]}                            判完以后下次他找机器人时给模型看一次（只写事实）
  pending  {QQ:日期: {...}}                               真删好友进行中（幂等：同一个人同一天只调一次）
  told     {事:时间}                                      v0.2.0 告诉过他的系统消息（判了什么 / 冷却到点 / 撤销了；同一件事只说一次）
  冷却 / 永久不理那一项里的 found：这段时间他找过机器人几次（私聊的 p、群里冲它说的 g；只记次数、不存原话）
文件读坏了：只读不写（不然从空表开始写回 = 名单、冷却全没了）—— 拉黑庭这时不立案，日志里写清楚。

核对引用 verify、封顶 trial_caps 照作者自己的机器人 v1.17.13：只往轻里压，不往重里加。
"""

import copy
import json
import os
import re
import time
import uuid
from pathlib import Path

from .block_judge import STAGE1, STAGE2, STAGE_RE, STAGE_VERDICTS, TAG_NOT, TAG_OWN

DEFAULTS = {
    "window_days": 7,  # 卷宗、核对只看这么多天里的话
    "keep_days": 30,  # 结案的案子、线索留多少天
    "cool_hours": 24.0,  # 判冷却：所有群和私聊不理他几小时
    "cool_min": 1,  # 一审判冷却 / 二审保留以后冷却：核实过的他冲机器人说的话至少几句（不够 → 放过）
    "retrial_quiet_sec": 300.0,  # 冷却中他说完、安静这么多秒 → 再审一次
    "retrial_max": 3,  # 一个案子冷却期间最多再审几次
    "retrial_min_total": 2,  # 再审判删除：冷却期间核实的（至少 1 句）+ 立案时核实过的 一共至少几句
    "second_min": 2,  # 二审判删除：这次卷宗里核实过的至少几句
    # （11-10 v0.3.1 审查 VP1-2：一阵连发约 6 分钟就从没前科到永久不理）不可逆的那一档（永久不理 / 删好友）另加两个门槛；不够的只冷却
    "delete_min_total": 3,  # 判删除：前后核实过的话一共至少几句（再审：立案时的 + 通知之后的；二审：上次冷却那个案子的 + 这次的）
    "delete_min_days": 2,  # 判删除：这些话至少分布在几天（本地时间，一天从凌晨 DAY_START_HOUR 点算）
    "delete_daily": 1,  # 每天最多判几个删除（超了只冷却）
    "court_daily": 10,  # 每天最多开几次庭（超了只冷却、不开庭）
    "appeal_days": 7,  # 同一个人几天里最多转一次申诉
}
LIGHT = {STAGE1: "放过", STAGE_RE: "不动", STAGE2: "保留"}
DAY_START_HOUR = 3  # （v0.3.1）判删除数「分布在几天」时一天从凌晨几点算：半夜连着说的（23:58 一句、00:03 两句）算同一天，不算跨了天
EV_MAX = 40  # 一个案子冷却期间最多收他几句（新的挤掉旧的）
# 冷却期间他的话里像道歉的：不能当再审删除的证据（只会少删、不会多删；法官自己也按「道歉不算越线」判）
SORRY = re.compile(
    r"对不起|对唔住|抱歉|我错了|是我不对|我的错|不好意思|原谅我|道歉|我检讨|sorry", re.I
)
KINDS = {"fake_admin": "冒充管理员"}
EMPTY = {
    "blocked": {},
    "cool": {},
    "cases": [],
    "cooled": {},
    "pardons": {},
    "marks": {},
    "deletes": [],
    "tries": [],
    "judged": {},
    "appeals": {},
    "notes": {},
    "pending": {},
    "told": {},
}
LIVE_STATES = ("审理中", "冷却中")


def _clip(t, n=40):
    return re.sub(r"\s+", " ", str(t or "")).strip()[:n]


def day_of(t=None):
    return time.strftime(
        "%Y-%m-%d", time.localtime(float(t if t is not None else time.time()))
    )


def md(t, fmt="%m-%d %H:%M"):
    try:
        return time.strftime(fmt, time.localtime(float(t or 0))) if t else ""
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _num(x):
    if isinstance(x, bool):
        return None
    if isinstance(x, int):
        return x
    m = re.fullmatch(r"\s*#?\s*(\d{1,4})\s*", x) if isinstance(x, str) else None
    return int(m.group(1)) if m else None


class BlockStore:
    def __init__(self, path, cfg=None, clock=time.time, warn=None):
        self.path = Path(path)
        self.cfg = dict(DEFAULTS)
        self.set_cfg(cfg or {})
        self.clock = clock
        self.warn = warn or (lambda m: None)
        self.broken = ""  # 文件读坏了：为什么（只读不写）
        self.d = self._fill({})
        self.load()

    # ───────── 配置 / 存档 ─────────
    def set_cfg(self, cfg):
        for k, v in (cfg or {}).items():
            if k in DEFAULTS and v is not None:
                try:
                    self.cfg[k] = type(DEFAULTS[k])(v)
                except (TypeError, ValueError):
                    pass

    def f(self, k):
        try:
            return float(self.cfg.get(k, DEFAULTS[k]))
        except (TypeError, ValueError):
            return float(DEFAULTS[k])

    def _now(self, now=None):
        return float(now if now is not None else self.clock())

    @staticmethod
    def _fill(d):
        for k, v in EMPTY.items():
            if not isinstance(d.get(k), type(v)):
                d[k] = type(v)()
        return d

    def load(self):
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self.d, self.broken = self._fill({}), ""
            return
        except OSError as e:
            self.broken = f"打不开（{type(e).__name__}）"
            self.warn(f"拉黑庭的存档 {self.path.name} 打不开：只读不写（{e!r}）"[:200])
            return
        try:
            d = json.loads(raw)
            if not isinstance(d, dict):
                raise ValueError("不是 dict")
        except ValueError as e:
            self.broken = "读坏了（不是能用的 JSON）"
            self.warn(
                f"拉黑庭的存档 {self.path.name} 读坏了：原文件没动、这次只读不写（拉黑庭不立案），修好或者删掉它再重载插件：{e!r}"[
                    :240
                ]
            )
            return
        self.d, self.broken = self._fill(d), ""

    def save(self):
        if self.broken:
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.d, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
            return True
        except OSError as e:
            self.warn(f"拉黑庭的存档写不进去：{e!r}"[:200])
            return False

    def ok(self):
        """能写吗（读坏了 → False）"""
        return not self.broken

    # ───────── 谁正在被挡 ─────────
    def is_blocked_forever(self, uid):
        return bool(uid) and str(uid) in self.d["blocked"]

    def cooling(self, uid, now=None):
        """正在冷却（含立案的占位）→ 冷却到几点；没在冷却 → 0"""
        c = self.d["cool"].get(str(uid or ""))
        try:
            u = float((c or {}).get("until") or 0)
        except (TypeError, ValueError, AttributeError):
            return 0
        return u if u > self._now(now) else 0

    def gate(self, uid, now=None):
        """他现在被挡着吗 → "blocked" 永久不理 / "hold" 审理中（占位）/ "cool" 冷却中 / None"""
        uid = str(uid or "")
        if not uid:
            return None
        if uid in self.d["blocked"]:
            return "blocked"
        if not self.cooling(uid, now):
            return None
        c = self.d["cool"].get(uid) or {}
        cs = self.case(str(c.get("case") or ""))
        return "hold" if cs and cs.get("state") == "审理中" else "cool"

    def blocked_list(self):
        return copy.deepcopy(self.d["blocked"])

    def cool_list(self, now=None):
        now = self._now(now)
        return {
            u: dict(e)
            for u, e in self.d["cool"].items()
            if isinstance(e, dict) and float(e.get("until") or 0) > now
        }

    # ───────── 名单 / 冷却 ─────────
    def add_block(self, uid, ent):
        if not self.ok():
            return False
        self.d["blocked"][str(uid)] = dict(ent)
        return self.save()

    def entry(self, uid, name, why, conv="", cites=(), now=None, case="", by="机器人"):
        return {
            "t": int(self._now(now)),
            "name": _clip(name, 20),
            "why": _clip(why, 40),
            "conv": str(conv or ""),
            "cites": [
                {"t": m.get("t"), "text": _clip(m.get("text"), 60)}
                for m in list(cites)[-5:]
            ],
            "by": by,
            "case": str(case or ""),
        }

    def start_cool(
        self, uid, name="", why="", conv="", now=None, hours=None, case="", by="机器人"
    ):
        """冷却（立案的占位也是它）→ 冷却到几点（写不进去 → 0）
        已经在冷却、到得更晚 → 照旧（不缩短）—— 除非那一项就是这个案子自己的占位（判完了：占位换成真的冷却，
        v0.1.0：占位等锁时会续到 10 分钟以后，冷却配得比这短时不能被占位顶掉）"""
        uid = str(uid or "")
        if not uid or not self.ok():
            return 0
        now = self._now(now)
        until = int(
            now + (self.f("cool_hours") if hours is None else float(hours)) * 3600
        )
        old = (
            self.d["cool"].get(uid) if isinstance(self.d["cool"].get(uid), dict) else {}
        )
        own = bool(case) and str(old.get("case") or "") == str(case)
        if float(old.get("until") or 0) > until and not own:
            return int(old["until"])
        self.d["cool"][uid] = {
            "t": int(now),
            "until": until,
            "name": _clip(name, 20),
            "why": _clip(why, 40),
            "conv": str(conv or ""),
            "case": str(case or ""),
            "by": by,
        }
        keep = now - self.f("keep_days") * 86400
        for k in [
            k
            for k, e in self.d["cool"].items()
            if not isinstance(e, dict) or float(e.get("until") or 0) < keep
        ]:
            self.d["cool"].pop(k, None)
        return until if self.save() else 0

    def end_cool(self, uid, case="", now=None):
        """法官判放过：占位马上结束（只结束这个案子的那一项）→ 结束了没有"""
        uid = str(uid or "")
        c = self.d["cool"].get(uid)
        if not isinstance(c, dict) or (case and str(c.get("case") or "") != str(case)):
            return False
        self.d["cool"].pop(uid, None)
        return self.save()

    def keep_hold(self, uid, case, until, now=None):
        """法官判完了、等锁的工夫：把这个案子的占位续到 until（案子还在审理中、没被撤销才续）→ 续上了没有"""
        uid, case = str(uid or ""), str(case or "")
        c = self.d["cool"].get(uid)
        if (
            not uid
            or not case
            or not isinstance(c, dict)
            or str(c.get("case") or "") != case
        ):
            return False
        if float(c.get("until") or 0) >= int(until):
            return False
        cs = self._case(case)
        if not cs or cs.get("state") != "审理中" or cs.get("pardoned"):
            return False
        c["until"] = int(until)
        return self.save()

    def unblock(self, uid, by="管理员", now=None):
        """撤销：解除永久不理 + 冷却、清掉「冷却过」、记赦免时间（之前的线索、案子不再算）→ 撤掉的那一项 / None
        还开着的案子（审理中 / 冷却中）收的证据作废、冷却中的结案；冷却早就到期的、只剩「冷却过」的也能撤；
        名单、冷却、冷却过里都没有他 → None"""
        uid = str(uid or "")
        now = self._now(now)
        if not uid or not self.ok():
            return None
        e = self.d["blocked"].pop(uid, None)
        c = (
            self.d["cool"].get(uid)
            if isinstance(self.d["cool"].get(uid), dict)
            else None
        )
        cd = self.d["cooled"].pop(uid, None)
        cd = cd if isinstance(cd, dict) else None
        live = c is not None and float(c.get("until") or 0) > now
        if e is None and c is None and cd is None:
            return None
        if live:
            self.d["cool"].pop(uid, None)
        if e is not None:
            out = dict(e, what="永久不理")
        else:
            out = dict(
                c or {},
                what="冷却" if live else ("冷却过" if cd else "冷却（早就到期了）"),
            )
        if cd is not None:
            out["cooled"] = dict(cd)
        self.d["pardons"][uid] = (
            int(now * 1000) / 1000
        )  # 往下取到毫秒（round 会往上进，撤销那一刻紧接着立的案会被当成「庭审期间撤销了」）
        for cs in self.d["cases"]:
            if (
                isinstance(cs, dict)
                and str(cs.get("uid")) == uid
                and cs.get("state") in LIVE_STATES
            ):
                cs["ev"] = []
                cs["pardoned"] = int(now)
                if cs.get("state") == "冷却中":
                    cs.update(state="结案", end="撤销了", closed=int(now))
        self.d["tries"].append(
            {
                "t": int(now),
                "uid": uid,
                "name": out.get("name") or "",
                "why": f"{by}撤销了{out['what']}",
                "res": "撤销",
            }
        )
        self.d["tries"] = self.d["tries"][-100:]
        self.save()
        return out

    def pardoned_at(self, uid):
        try:
            return float(self.d["pardons"].get(str(uid)) or 0)
        except (TypeError, ValueError):
            return 0.0

    # ───────── 线索（只当线索，不算门槛）─────────
    def mark(self, uid, kinds, conv="", text="", now=None, name="", mid=None):
        uid = str(uid or "")
        ks = [kinds] if isinstance(kinds, str) else list(kinds or [])
        ks = [k for k in dict.fromkeys(ks) if k in KINDS]
        if not uid or not ks or not self.ok():
            return False
        now = self._now(now)
        ms = self.d["marks"].setdefault(uid, [])
        if mid is not None:
            hit = next(
                (
                    m
                    for m in ms
                    if m.get("mid") is not None and str(m.get("mid")) == str(mid)
                ),
                None,
            )
            if hit:
                new = [k for k in ks if k not in (hit.get("kinds") or [])]
                if not new:
                    return False
                hit["kinds"] = list(hit.get("kinds") or []) + new
                return self.save()
        elif any(
            set(ks) <= set(m.get("kinds") or []) and now - float(m.get("t") or 0) < 600
            for m in ms
        ):
            return False
        rec = {
            "t": int(now),
            "kinds": ks,
            "conv": str(conv or ""),
            "text": _clip(text),
            "name": _clip(name, 20),
        }
        if mid is not None:
            rec["mid"] = str(mid)
        ms.append(rec)
        keep = now - self.f("keep_days") * 86400
        ms[:] = [m for m in ms if float(m.get("t") or 0) >= keep][-60:]
        return self.save()

    def marks(self, uid, now=None, days=None):
        """他 days 天（默认 window_days）里的线索（撤销之前的不算）"""
        now = self._now(now)
        lim = max(
            now - (self.f("window_days") if days is None else float(days)) * 86400,
            self.pardoned_at(uid) + 0.001,
        )
        return [
            dict(m)
            for m in self.d["marks"].get(str(uid), [])
            if isinstance(m, dict) and float(m.get("t") or 0) >= lim
        ]

    # ───────── 核对引用 / 封顶 ─────────
    def verify(self, cites, table, uid, now=None, pardon=None):
        """法官引用的编号 → 核实过的那几条：编号在卷宗里、那条是他本人发的、冲机器人说的（标 本人→机器人）、
        在 window_days 天内、在撤销之后；同一条只算一次（同一个消息 id；或者同一个对话 60 秒内一字不差的）"""
        now = self._now(now)
        lim = now - self.f("window_days") * 86400
        pd = self.pardoned_at(uid) if pardon is None else float(pardon or 0)
        tb = {}
        for k, v in (table or {}).items():
            n = _num(k)
            if n is not None and isinstance(v, dict):
                tb[n] = v
        out, seen, mids = [], set(), set()
        for c in cites or []:
            n = _num(c)
            m = tb.get(n) if n is not None else None
            if not m or n in seen:
                continue
            if str(m.get("uid")) != str(uid) or m.get("tag") != TAG_OWN:
                continue
            if m.get("pre"):  # （11-10 v0.3.1 审查 VP1-2）通知发到他手里之前说的：只当背景，被引用了也不算
                continue
            try:
                t = float(m.get("t") or 0)
            except (TypeError, ValueError):
                continue
            if t < lim or t <= pd or t > now + 120:
                continue
            if m.get("mid") is not None and str(m.get("mid")) in mids:
                continue
            core = re.sub(r"\s+", "", str(m.get("text") or ""))
            if any(
                str(x.get("conv")) == str(m.get("conv"))
                and re.sub(r"\s+", "", str(x.get("text") or "")) == core
                and abs(float(x.get("t") or 0) - t) <= 60
                for x in out
            ):
                continue
            seen.add(n)
            if m.get("mid") is not None:
                mids.add(str(m.get("mid")))
            out.append(dict(m, n=n))
        return out

    def delete_lack(self, ok, prior=0, prior_ts=(), told=False):
        """（11-10 v0.3.1 审查 VP1-2）判删除（永久不理 / 删好友，不可逆的那一档）另加的门槛 → 差在哪（一个都不差 → []）
        told：判决 / 冷却的通知真的发到他手里了（再审：这个案子的；二审：上次冷却的）—— 没发到 = 没有「通知之后」
        前后核实过的话（这次的 ok + 以前核实过的 prior 句）一共 ≥ delete_min_total 句、分布在 ≥ delete_min_days 天
        （prior_ts：以前核实过的那几句是哪天说的；没给的那几句算不出是哪天 → 不算天数；一天从凌晨 DAY_START_HOUR 点算）"""
        lack = []
        if not told:
            lack.append("判决 / 冷却的通知没发到他手里（没有「通知之后」）")
        tot = max(1, int(self.f("delete_min_total")))
        dmin = max(1, int(self.f("delete_min_days")))
        n = len(ok) + max(0, int(prior or 0))
        ts = [m.get("t") for m in ok] + list(prior_ts or ())
        days = set()
        for t in ts:
            try:
                if float(t or 0) > 0:
                    days.add(day_of(float(t) - DAY_START_HOUR * 3600))
            except (TypeError, ValueError):
                continue
        if n < tot:
            lack.append(f"核实过的话前后一共 {n} 句（要至少 {tot} 句）")
        if len(days) < dmin:
            lack.append(f"这些话分布在 {len(days)} 天（要至少 {dmin} 天）")
        return lack

    def trial_caps(self, stage, verdict, ok, prior=0, prior_ts=(), told=False):
        """法官的判词 + 核实过的引用 → 最后怎么办（只往轻里压）
        一审：冷却 要核实 ≥ cool_min 句，不够 → 放过
        再审：删除 要冷却期间核实 ≥ 1 句（像道歉的不算）、加上立案时核实过的（prior）一共 ≥ retrial_min_total 句，不够 → 不动
        二审：删除 要这次核实 ≥ second_min 句，不够 → 保留；保留：这次核实过 ≥ cool_min 句 → 冷却，一句都没有 → 放过
        v0.3.1：再审 / 二审的删除还要过 delete_lack（通知发到他手里了、前后一共几句、分布在几天；prior / prior_ts / told 是给它的）；
               不够 → 再审不动（照旧冷却）、二审保留（→ 冷却 / 放过），why 里写清差在哪
        判词不认识 → 按轻的那个
        → {"result", "verdict", "why", "n", "days", "ok"}"""
        ok = [m for m in ok or [] if isinstance(m, dict)]
        stage = stage if stage in STAGE_VERDICTS else STAGE1
        vd = verdict if verdict in STAGE_VERDICTS[stage] else LIGHT[stage]
        sorry = []
        if stage == STAGE_RE:
            sorry = [
                m
                for m in ok
                if m.get("sorry") or SORRY.search(str(m.get("text") or ""))
            ]
            ok = [m for m in ok if m not in sorry]
        n = len(ok)
        days = len({day_of(float(m.get("t") or 0)) for m in ok})
        cmin = max(1, int(self.f("cool_min")))
        why, res = [], vd
        if stage == STAGE_RE and vd == "删除":
            tot = max(2, int(self.f("retrial_min_total")))
            pr = max(0, int(prior or 0))
            if n < 1 or n + pr < tot:
                why.append(
                    (
                        f"冷却期间的话核实 {n} 句（删除要至少 1 句）"
                        if n < 1
                        else f"冷却期间核实 {n} 句 + 立案时 {pr} 句（删除一共要 {tot} 句）"
                    )
                    + (f"；像道歉的 {len(sorry)} 句不算" if sorry else "")
                )
                res = "不动"
        elif stage == STAGE2:
            if vd == "删除":
                smin = max(2, int(self.f("second_min")))
                if n < smin:
                    why.append(f"这次核实 {n} 句（删除要 {smin} 句）")
                    res = "保留"
        elif vd == "冷却" and n < cmin:
            why.append(f"核实 {n} 句（冷却要 {cmin} 句）")
            res = "放过"
        if res == "删除":  # （11-10 v0.3.1 审查 VP1-2）不可逆的那一档另加门槛：不够的只冷却
            lack = self.delete_lack(ok, prior, prior_ts, told)
            if lack:
                why.append("法官判删除，但永久不理的门槛没到：" + "、".join(lack) + " → 只冷却")
                res = LIGHT[stage]
        if stage == STAGE2 and res == "保留":
            res = "冷却" if n >= cmin else "放过"
        return {
            "result": res,
            "verdict": vd,
            "why": "；".join(why),
            "n": n,
            "days": days,
            "ok": ok,
        }

    def deleted_today(self, now=None):
        """今天判了几个删除（删好友、清数据 真做了 / 本来会做 / 不是好友 都算一个；真删好友进行中的也算）"""
        day = day_of(self._now(now))
        return sum(
            1 for x in self.d["deletes"] if isinstance(x, dict) and x.get("day") == day
        ) + sum(
            1
            for x in self.d["pending"].values()
            if isinstance(x, dict)
            and x.get("day") == day
            and x.get("state") == "进行中"
        )

    # ───────── 案子 ─────────
    def new_cid(self, now=None):
        return (
            time.strftime("%m%d-%H%M", time.localtime(self._now(now)))
            + "-"
            + uuid.uuid4().hex[:4]
        )

    def _case(self, cid):
        return next(
            (c for c in self.d["cases"] if isinstance(c, dict) and c.get("id") == cid),
            None,
        )

    def case(self, cid):
        c = self._case(cid)
        return copy.deepcopy(c) if c else None

    def _sweep(self, now):
        k = 0
        for c in self.d["cases"]:
            if (
                isinstance(c, dict)
                and c.get("state") == "冷却中"
                and float(c.get("cool_until") or 0) <= now
            ):
                c.update(
                    state="结案",
                    end=c.get("end") or "冷却到点",
                    closed=int(float(c.get("cool_until") or now)),
                )
                k += 1
        return k

    def sweep(self, now=None):
        """冷却中的案子到点了 → 结案（冷却到点）→ 结了几个"""
        k = self._sweep(self._now(now))
        if k:
            self.save()
        return k

    def open_case(
        self,
        uid,
        name="",
        why="",
        conv="",
        now=None,
        cool_until=0,
        stage="",
        evok=False,
        cid="",
        by="机器人",
    ):
        """立案（状态「审理中」）→ 案号；写不进去 → \"\"
        evok：冷却中收不收证据、再不再审（他不在白名单里、再审开着）"""
        if not self.ok():
            return ""
        now = self._now(now)
        cid = str(cid or self.new_cid(now))
        rec = {
            "id": cid,
            "uid": str(uid),
            "name": _clip(name, 20),
            "t": int(now),
            "conv": str(conv or ""),
            "why": _clip(why, 40),
            "by": by,
            "state": "审理中",
            "cool_until": int(cool_until or 0),
            "stage": str(stage or STAGE1),
        }
        if evok:
            rec["evok"] = True
        self._sweep(now)
        self.d["cases"].append(rec)
        keep = now - self.f("keep_days") * 86400
        self.d["cases"] = [
            c
            for c in self.d["cases"]
            if isinstance(c, dict)
            and (float(c.get("t") or 0) >= keep or c.get("state") in LIVE_STATES)
        ][-200:]
        return cid if self.save() else ""

    def update_case(self, cid, **fields):
        c = self._case(cid)
        if not c:
            return False
        c.update(fields)
        return self.save()

    def close_case(self, cid, **fields):
        c = self._case(cid)
        if not c:
            return False
        c.update(fields)
        c["state"] = "结案"
        c["closed"] = int(self.clock())
        return self.save()

    def cases(self, uid=None, after=None, n=None):
        out = [copy.deepcopy(c) for c in self.d["cases"] if isinstance(c, dict)]
        if uid is not None:
            out = [c for c in out if str(c.get("uid")) == str(uid)]
        if after:
            out = [c for c in out if float(c.get("t") or 0) > float(after)]
        return out[-int(n) :] if n else out

    def cases_on(self, day=None):
        day = day or day_of(self.clock())
        return sum(
            1
            for c in self.d["cases"]
            if isinstance(c, dict) and day_of(float(c.get("t") or 0)) == day
        )

    def stuck_cases(self):
        """开机：卡在「审理中」的（上次庭审到一半重启了）"""
        return [
            copy.deepcopy(c)
            for c in self.d["cases"]
            if isinstance(c, dict) and c.get("state") == "审理中"
        ]

    def open_case_of(self, uid, now=None):
        """他还在审理中（占位）的案子（一个小时以前立的还挂着 = 收尾没写上，开机会收 → 不算）"""
        now = self._now(now)
        return next(
            (
                copy.deepcopy(c)
                for c in reversed(self.d["cases"])
                if isinstance(c, dict)
                and str(c.get("uid")) == str(uid)
                and c.get("state") == "审理中"
                and now - float(c.get("t") or 0) < 3600
            ),
            None,
        )

    def cooling_cases(self):
        return [
            copy.deepcopy(c)
            for c in self.d["cases"]
            if isinstance(c, dict) and c.get("state") == "冷却中"
        ]

    # ───────── 冷却过 ─────────
    def mark_cooled(self, uid, until, case="", now=None):
        uid = str(uid or "")
        if not uid or not until or not self.ok():
            return False
        now = self._now(now)
        old = (
            self.d["cooled"].get(uid)
            if isinstance(self.d["cooled"].get(uid), dict)
            else {}
        )
        self.d["cooled"][uid] = {
            "t": int(float(old.get("t") or now)),
            "last": int(float(until)),
            "case": str(case or ""),
            "n": int(old.get("n") or 0) + 1,
        }
        return self.save()

    def cooled_of(self, uid, now=None):
        """他算不算「冷却过」：冷却已经到点了（last ≤ 现在）、在最近一次撤销之后 → 那一项 / None"""
        uid = str(uid or "")
        e = self.d["cooled"].get(uid)
        if not uid or not isinstance(e, dict):
            return None
        try:
            last = float(e.get("last") or 0)
        except (TypeError, ValueError):
            return None
        if not last or last > self._now(now) or last <= self.pardoned_at(uid):
            return None
        return dict(e)

    def cooled_list(self, now=None):
        now = self._now(now)
        out = {}
        for u, e in self.d["cooled"].items():
            try:
                if isinstance(e, dict) and 0 < float(e.get("last") or 0) <= now:
                    out[u] = dict(e)
            except (TypeError, ValueError):
                continue
        return out

    # ───────── 冷却中收证据 / 再审 ─────────
    def ev_case(self, uid, now=None):
        """他这会儿冲机器人说的话要不要当证据收 → 收进哪个案子（副本）/ None
        收：占位 / 冷却那一项还在；他最近的那个案子收证据（evok）、没删过数据；审理中，或者冷却中、冷却还没到点、再审还没用完"""
        uid = str(uid or "")
        now = self._now(now)
        if not uid or not self.cooling(uid, now):
            return None
        c = next(
            (
                x
                for x in reversed(self.d["cases"])
                if isinstance(x, dict) and str(x.get("uid")) == uid
            ),
            None,
        )
        if not c or c.get("wiped") or not c.get("evok"):
            return None
        if c.get("state") == "审理中" or (
            c.get("state") == "冷却中"
            and float(c.get("cool_until") or 0) > now
            and len(c.get("retrials") or []) < max(0, int(self.f("retrial_max")))
        ):
            return copy.deepcopy(c)
        return None

    def add_ev(self, cid, item, now=None):
        """审理中 / 冷却中他冲机器人说的一句 → 记进案子（只当证据）→ 记上了没有
        item：{t, mid, conv, tag, text}；同一个消息 id 只记一次；最多 EV_MAX 条；记下最后一句的时间 ev_last（去抖）；像道歉的标 sorry"""
        now = self._now(now)
        c = self._case(cid)
        if (
            not c
            or c.get("wiped")
            or not c.get("evok")
            or c.get("state") not in LIVE_STATES
        ):
            return False
        ev = [e for e in c.get("ev") or [] if isinstance(e, dict)]
        mid = item.get("mid")
        if mid is not None and any(
            e.get("mid") is not None and str(e.get("mid")) == str(mid) for e in ev
        ):
            return False
        t = float(item.get("t") or now)
        text = _clip(item.get("text"), 200)
        rec = {
            "t": round(t, 3),
            "conv": str(item.get("conv") or ""),
            "tag": str(item.get("tag") or TAG_NOT),
            "text": text,
        }
        if item.get("where"):
            rec["where"] = str(item["where"])[:8]
        if mid is not None:
            rec["mid"] = str(mid)
        if SORRY.search(text):
            rec["sorry"] = True
        # （11-10 v0.3.1 审查 VP1-2）占位期间 / 通知发到他手里之前说的：pre = True，只当背景（不编号、不能引用、不为它再审）；
        #   每条都写上这个键（v0.3.0 收的没有这个键：retrial_begin 里照通知时间补）
        ta = self.case_told_at(cid)
        rec["pre"] = bool(not ta or t <= ta)
        ev.append(rec)
        c["ev"] = ev[-EV_MAX:]
        c["ev_last"] = round(max(t, float(c.get("ev_last") or 0)), 3)
        return self.save()

    @staticmethod
    def ev_due(c):
        """这个案子有没有还没审过的、他冲机器人说的话 —— 有才再审（别的照样收着当背景，不为它们开庭）
        v0.3.1：通知发到他手里之前说的（标 pre）也只是背景，不为它开庭"""
        return any(
            isinstance(e, dict) and not e.get("seen") and e.get("tag") == TAG_OWN and not e.get("pre")
            for e in (c or {}).get("ev") or []
        )

    def retrial_begin(self, cid, now=None):
        """再审开始：没审过的证据标上第几次（seen）、记「正在再审」rt_run（重启时按不动收尾）→ 案子副本 / None"""
        now = self._now(now)
        c = self._case(cid)
        if not c or c.get("wiped") or c.get("state") != "冷却中":
            return None
        k = len(c.get("retrials") or []) + 1
        ta = self.case_told_at(cid)
        for e in c.get("ev") or []:  # （11-10 v0.3.1 审查 VP1-2）v0.3.0 收的证据上没有 pre 这个键：照通知发到他手里的时间补上
            if isinstance(e, dict) and "pre" not in e:
                e["pre"] = bool(not ta or float(e.get("t") or 0) <= ta)
        if not self.ev_due(c) or k > max(0, int(self.f("retrial_max"))):
            return None
        for e in c.get("ev") or []:
            if isinstance(e, dict) and not e.get("seen"):
                e["seen"] = k
        c["rt_run"] = round(now, 3)
        self.save()
        return copy.deepcopy(c)

    def retrial_end(self, cid, rec, **fields):
        c = self._case(cid)
        if not c:
            return False
        c["retrials"] = (
            [r for r in c.get("retrials") or [] if isinstance(r, dict)] + [dict(rec)]
        )[-10:]
        c.pop("rt_run", None)
        c.update(fields)
        return self.save()

    # ───────── 真删好友：进行中（幂等）─────────
    def pending_start(self, uid, day, rec=None, now=None):
        k = f"{uid}:{day}"
        if k in self.d["pending"] or not self.ok():
            return False
        self.d["pending"][k] = dict(
            rec or {}, uid=str(uid), day=str(day), t=int(self._now(now)), state="进行中"
        )
        if len(self.d["pending"]) > 100:
            for x in sorted(
                self.d["pending"],
                key=lambda x: float((self.d["pending"][x] or {}).get("t") or 0),
            )[: len(self.d["pending"]) - 100]:
                if (self.d["pending"][x] or {}).get("state") != "进行中":
                    self.d["pending"].pop(x, None)
        return self.save()

    def pending_end(self, uid, day, result, err=""):
        e = self.d["pending"].get(f"{uid}:{day}")
        if not isinstance(e, dict):
            return False
        e.update(
            state="完了", result=str(result), err=_clip(err, 80), end=int(self.clock())
        )
        return self.save()

    def pending_open(self):
        return [
            dict(v, key=k)
            for k, v in self.d["pending"].items()
            if isinstance(v, dict) and v.get("state") == "进行中"
        ]

    # ───────── 记一笔 ─────────
    def note_try(self, uid, name, why, res, now=None, by="机器人"):
        self.d["tries"].append(
            {
                "t": int(self._now(now)),
                "uid": str(uid),
                "name": _clip(name, 20),
                "why": _clip(why, 40),
                "by": by,
                "res": _clip(res, 60),
            }
        )
        self.d["tries"] = self.d["tries"][-100:]
        self.save()

    def note_delete(self, uid, name, why, now=None, case="", friend="", wipe="", ms=()):
        """判删除执行了一次（删好友 / 清数据各是什么结果）—— 每天的上限数它"""
        now = self._now(now)
        self.d["deletes"].append(
            {
                "t": int(now),
                "day": day_of(now),
                "uid": str(uid),
                "name": _clip(name, 20),
                "why": _clip(why, 40),
                "case": str(case or ""),
                "friend": _clip(friend, 40),
                "wipe": _clip(wipe, 40),
                "evidence": "；".join(
                    f"{md(m.get('t'))}「{_clip(m.get('text'), 40)}」"
                    for m in list(ms)[-4:]
                ),
            }
        )
        self.d["deletes"] = self.d["deletes"][-100:]
        return self.save()

    def note_delete_update(self, case, **fields):
        x = next(
            (
                x
                for x in reversed(self.d["deletes"])
                if isinstance(x, dict) and str(x.get("case") or "") == str(case)
            ),
            None,
        )
        if not x:
            return False
        x.update({k: _clip(v, 40) for k, v in fields.items()})
        return self.save()

    def judged_add(self, uid, what, now=None):
        """被判过几次（冷却 / 删除）→ 现在几次"""
        uid = str(uid)
        j = (
            self.d["judged"].get(uid)
            if isinstance(self.d["judged"].get(uid), dict)
            else {}
        )
        j = {
            "n": int(j.get("n") or 0) + 1,
            "last": int(self._now(now)),
            "what": str(what),
        }
        self.d["judged"][uid] = j
        self.save()
        return j["n"]

    def judged_of(self, uid):
        j = self.d["judged"].get(str(uid))
        return int((j or {}).get("n") or 0) if isinstance(j, dict) else 0

    # ───────── v0.2.0 告诉过他什么 / 他找过几次 / 刚到点的冷却 ─────────
    def was_told(self, key):
        return bool(self.d["told"].get(str(key)))

    def mark_told(self, key, now=None):
        """记下「这件事告诉过他了」（同一件事只说一次）→ 记上了没有；keep_days 天以前的清掉"""
        if not self.ok():
            return False
        now = self._now(now)
        self.d["told"][str(key)] = int(now)
        keep = now - self.f("keep_days") * 86400
        for k in [k for k, v in self.d["told"].items() if not isinstance(v, (int, float)) or v < keep]:
            self.d["told"].pop(k, None)
        return self.save()

    def told_at(self, key):
        """这件事上次告诉他是几点（没告诉过 → 0）"""
        try:
            return float(self.d["told"].get(str(key)) or 0)
        except (TypeError, ValueError):
            return 0.0

    # （11-10 v0.3.1 审查 VP1-2：只有判决 / 冷却的通知真的发到他手里以后他还继续说的话，才能当加重的依据）
    def case_told_at(self, cid):
        """这个案子「判了冷却」的通知发到他手里是几点 → 时间戳；没发出去 / 没有这个案子 → 0
        v0.3.0 判的案子上没记这个时间：用「告诉过他」那一行（只精确到秒 → 往后取 1 秒，同一秒里说的算通知之前）"""
        cid = str(cid or "")
        c = self._case(cid) if cid else None
        try:
            t = float((c or {}).get("told_t") or 0)
        except (TypeError, ValueError):
            t = 0.0
        if not t and cid:
            t0 = self.told_at(f"case:{cid}:冷却")
            t = t0 + 1 if t0 else 0.0
        return t

    def mark_case_told(self, cid, now=None):
        """「判了冷却」的通知发到他手里了 → 案子上记时间（told_t，只记第一次）；他的「冷却过」是这个案子的 → 也记上 told（二审看）→ 记上了没有"""
        c = self._case(str(cid or ""))
        if not c or not self.ok():
            return False
        now = self._now(now)
        if not c.get("told_t"):
            c["told_t"] = round(now, 6)
        cd = self.d["cooled"].get(str(c.get("uid")))
        if isinstance(cd, dict) and str(cd.get("case") or "") == str(cid) and not cd.get("told"):
            cd["told"] = int(now)
        return self.save()

    def found(self, uid, private=True, now=None):
        """冷却中 / 审理中 / 永久不理的人又找机器人了 → 那一项的 found 记一次（私聊 p、群里 g；只记次数）→ 记上了没有"""
        uid = str(uid or "")
        e = self.d["blocked"].get(uid)
        if not isinstance(e, dict):
            e = self.d["cool"].get(uid) if self.cooling(uid, now) else None
        if not isinstance(e, dict) or not self.ok():
            return False
        f = e.get("found") if isinstance(e.get("found"), dict) else {}
        k = "p" if private else "g"
        f[k] = int(f.get(k) or 0) + 1
        e["found"] = f
        return self.save()

    def cool_ends(self, now=None, within=12 * 3600):
        """within 秒内到点了的冷却（含没判成的、二审保留后的；占位那种 —— 案子还在审理 / 放过结束的 —— 不算）→ [(QQ, 那一项)]"""
        now = self._now(now)
        out = []
        for u, e in self.d["cool"].items():
            if not isinstance(e, dict):
                continue
            until = float(e.get("until") or 0)
            if not (now - within < until <= now):
                continue
            cs = self._case(str(e.get("case") or "")) if e.get("case") else None
            if cs and cs.get("state") == "审理中":
                continue
            out.append((u, dict(e)))
        return out

    def cool_end_of(self, uid, now=None, within=12 * 3600):
        """他的冷却 within 秒内到点了（规矩跟 cool_ends 一样）→ 那一项 / None"""
        e = self.d["cool"].get(str(uid or ""))
        if not isinstance(e, dict):
            return None
        now = self._now(now)
        until = float(e.get("until") or 0)
        if not (now - within < until <= now):
            return None
        cs = self._case(str(e.get("case") or "")) if e.get("case") else None
        if cs and cs.get("state") == "审理中":
            return None
        return dict(e)

    # ───────── 给模型的一行（下次他找机器人时给一次）─────────
    def add_note(self, uid, text, now=None):
        uid = str(uid or "")
        if not uid or not text or not self.ok():
            return False
        xs = [x for x in self.d["notes"].get(uid, []) if isinstance(x, dict)]
        xs.append({"t": int(self._now(now)), "text": _clip(text, 160)})
        self.d["notes"][uid] = xs[-3:]
        if len(self.d["notes"]) > 500:
            for k in sorted(
                self.d["notes"],
                key=lambda k: max(
                    [float(x.get("t") or 0) for x in self.d["notes"][k]] or [0]
                ),
            )[:100]:
                self.d["notes"].pop(k, None)
        return self.save()

    def take_notes(self, uid, now=None, days=7):
        """他这一轮要给模型看的（7 天内的；给过就删）"""
        uid = str(uid or "")
        xs = self.d["notes"].pop(uid, None)
        if not xs:
            return []
        self.save()
        now = self._now(now)
        return [
            x["text"]
            for x in xs
            if isinstance(x, dict)
            and x.get("text")
            and now - float(x.get("t") or 0) <= days * 86400
        ]

    # ───────── 申诉 ─────────
    def appeal_check(self, uid, cid, now=None):
        """能不能转这一次申诉 → (能, 为什么不能)：同一个案子只转一次；同一个人 appeal_days 天里最多转一次"""
        now = self._now(now)
        xs = [x for x in self.d["appeals"].get(str(uid), []) if isinstance(x, dict)]
        if cid and any(str(x.get("case") or "") == str(cid) for x in xs):
            return False, "这个案子已经转过一次申诉了"
        days = max(0.0, self.f("appeal_days"))
        last = max([float(x.get("t") or 0) for x in xs] or [0])
        if days and now - last < days * 86400:
            return False, f"{days:g} 天里已经转过一次申诉了"
        return True, ""

    def add_appeal(self, uid, cid, text, src, now=None, name="", state=""):
        uid = str(uid or "")
        if not uid or not self.ok():
            return False
        xs = [x for x in self.d["appeals"].get(uid, []) if isinstance(x, dict)]
        xs.append(
            {
                "t": int(self._now(now)),
                "case": str(cid or ""),
                "text": _clip(text, 200),
                "src": str(src),
                "name": _clip(name, 20),
                "state": str(state),
            }
        )
        self.d["appeals"][uid] = xs[-10:]
        return self.save()

    def appeals(self, n=30):
        rows = []
        for u, xs in self.d["appeals"].items():
            for x in xs or []:
                if isinstance(x, dict):
                    rows.append(dict(x, uid=u))
        rows.sort(key=lambda r: -float(r.get("t") or 0))
        return rows[:n]

    # ───────── 删数据 ─────────
    def forget(self, uid, names=()):
        """删他的数据：线索、申诉、给模型的话整条删；名单 / 冷却里他那一项留着（不然删数据 = 解封）但名字、原因、原话抹掉；
        「冷却过」只留号和时间（还算冷却过 —— 删数据不等于洗白；撤销才清）；他的案子只留号 / 时间 / 状态 / 判词 / 结果（证据、原话、理由都不留）；
        别人那几条里出现的他的名字换「某人」→ 动了几条"""
        uid = str(uid or "")
        if not uid or not self.ok():
            return 0
        d = self.d
        n = (
            len(d["marks"].pop(uid, []) or [])
            + len(d["appeals"].pop(uid, []) or [])
            + len(d["notes"].pop(uid, []) or [])
        )
        e = d["blocked"].get(uid)
        if isinstance(e, dict):
            d["blocked"][uid] = {
                "t": e.get("t"),
                "by": e.get("by") or "机器人",
                "why": "（删数据时抹掉了）",
                "name": "",
                "conv": "",
                "cites": [
                    {"t": m.get("t")}
                    for m in e.get("cites") or []
                    if isinstance(m, dict)
                ],
                **({"case": e["case"]} if e.get("case") else {}),
            }
            n += 1
        c = d["cool"].get(uid)
        if isinstance(c, dict):
            d["cool"][uid] = {
                "t": c.get("t"),
                "until": c.get("until"),
                "by": c.get("by") or "机器人",
                "why": "（删数据时抹掉了）",
                "name": "",
                "conv": "",
                **({"case": c["case"]} if c.get("case") else {}),
            }
            n += 1
        cd = d["cooled"].get(uid)
        if isinstance(cd, dict):
            d["cooled"][uid] = {k: cd.get(k) for k in ("t", "last", "told") if k in cd}  # v0.3.1：told = 那次冷却通知过他（二审看；删数据不等于洗白）
            n += 1
        nm = [str(x) for x in names or [] if x and len(str(x)) >= 2]

        def sc(s):
            s = str(s or "")
            for x in nm:
                s = s.replace(x, "某人")
            return s

        for i, cs in enumerate(d["cases"]):
            if not isinstance(cs, dict):
                continue
            if str(cs.get("uid")) == uid:
                d["cases"][i] = {
                    k: cs.get(k)
                    for k in (
                        "id",
                        "uid",
                        "t",
                        "state",
                        "verdict",
                        "result",
                        "cool_until",
                        "closed",
                        "n_ok",
                        "days",
                        "stage",
                        "end",
                        "by",
                        "evok",
                    )
                    if k in cs
                }
                d["cases"][i]["wiped"] = int(self.clock())
                n += 1
            else:
                for f in ("why", "reason", "result_why", "name"):
                    if f in cs:
                        cs[f] = sc(cs[f])
                for m in (cs.get("quotes") or []) + (cs.get("ev") or []):
                    if isinstance(m, dict):
                        m["text"] = sc(m.get("text"))
                for r in cs.get("retrials") or []:
                    if isinstance(r, dict):
                        for f in ("reason", "why"):
                            if f in r:
                                r[f] = sc(r[f])
        for k, v in list(d["pending"].items()):
            if isinstance(v, dict) and str(v.get("uid")) == uid:
                d["pending"][k] = {
                    f: v.get(f)
                    for f in ("uid", "day", "t", "state", "result", "end")
                    if f in v
                }
                n += 1
        for k in ("tries", "deletes"):
            for x in d.get(k) or []:
                if not isinstance(x, dict):
                    continue
                if str(x.get("uid")) == uid:
                    x.update(name="", why="（删数据时抹掉了）", evidence="")
                    n += 1
                else:
                    for f in ("why", "evidence", "res", "name"):
                        if f in x:
                            x[f] = sc(x[f])
        for ms in d["marks"].values():
            for m in ms:
                m["text"], m["name"] = sc(m.get("text")), sc(m.get("name"))
        for xs in d["appeals"].values():
            for x in xs:
                if isinstance(x, dict):
                    x["text"] = sc(x.get("text"))
        for k2, e2 in d["blocked"].items():
            if isinstance(e2, dict) and k2 != uid:
                e2["why"] = sc(e2.get("why"))
                for m in e2.get("cites") or []:
                    if isinstance(m, dict) and "text" in m:
                        m["text"] = sc(m.get("text"))
        for k2, c2 in d["cool"].items():
            if isinstance(c2, dict) and k2 != uid:
                c2["why"] = sc(c2.get("why"))
        self.save()
        return n

    # ───────── 面板 ─────────
    def view(self, now=None):
        now = self._now(now)
        d = self.d
        return {
            "blocked": copy.deepcopy(d["blocked"]),
            "cooling": self.cool_list(now),
            "cooled": self.cooled_list(now),
            "cases": copy.deepcopy(list(reversed(d["cases"][-30:]))),
            "deletes": copy.deepcopy(d["deletes"][-30:]),
            "tries": copy.deepcopy(d["tries"][-30:]),
            "appeals": self.appeals(30),
            "judged": copy.deepcopy(d["judged"]),
            "broken": self.broken,
        }
