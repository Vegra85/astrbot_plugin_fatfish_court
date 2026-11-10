#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v0.3.1 的单元测试（不用装 AstrBot、不连外网，几秒钟）：python tests/test_court_031.py [插件目录 默认上一层]
  ① 拉黑庭 · 通知之后才算：占位那几分钟 / 通知发到他手里之前说的话只当背景（单列【判决前说的】、不编号、不能引用、不为它再审）；
     通知没发出去的（发送失败 / 没有通知渠道 / 关了「判完私聊告诉他」）→ 没有「通知之后」→ 判不到永久不理
  ② 拉黑庭 · 永久不理另加门槛：核实过的话前后一共至少 3 句、分布在至少 2 天（两个配置项）；不够的只冷却，记录里写清差在哪
  ③ 关掉「二审」= 再审也判不到永久不理
  ④ 纠错庭 · 判词只认固定的三个写法：机器人错了 / 机器人没错 / 说不准；别的写法 = 没判成（不认错、不记教训）
  ⑤ 「忘记我」只按 QQ 号删教训；按名片连带删的时候不看「X指出、核对过」这种固定格式的字
  ⑥ main.py 这一层（用一个假的 astrbot 包顶上，不是真 AstrBot）：通知发没发出去怎么认、判词读不出时不认错不记教训、忘记我、新配置接上了
法官、QQ 都是假的；假法官一律往最重里判、把能引用的全引用上 —— 看代码这一层拦不拦得住。人名、号码都是编的。
拿 v0.3.0 的代码跑：带「回退检查」的那些项没过。"""

import asyncio
import importlib
import json
import re
import sys
import tempfile
import time
import types
from pathlib import Path

d = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent).resolve()
sys.path.insert(0, str(d))
from core import block_flow as bf  # noqa: E402
from core import block_judge as bj  # noqa: E402
from core import block_log as bl  # noqa: E402
from core import block_store as bs  # noqa: E402
from core import court as ct  # noqa: E402
from core import lessons as ls  # noqa: E402

RES = []


def check(c, m, why=""):
    RES.append((bool(c), m))
    print(("  [过]   " if c else "  [没过] ") + m + ("" if c or not why else f"\n           {str(why)[:700]}"))


def safe(fn, *a, **k):
    """老代码没有的参数 / 函数：不让整个脚本崩，这一项记成没过"""
    try:
        return fn(*a, **k)
    except Exception as e:  # noqa: BLE001
        return {"result": f"出错：{e!r}"[:120], "why": "", "err": True}


G = "aiocqhttp:GroupMessage:7501"
A, B, C, D, E, F, ADMIN, WHITE, AI = "10001", "10002", "10003", "10004", "10005", "10006", "10009", "10007", "50005"
_lt = time.localtime()
NOON = time.mktime((_lt.tm_year, _lt.tm_mon, _lt.tm_mday, 12, 0, 0, 0, 0, -1))  # 今天中午（测试里的「现在」，不跨午夜）
DAY = 86400
SCHEMA = json.loads((d / "_conf_schema.json").read_text(encoding="utf-8"))
BK = SCHEMA["block"]["items"]


class FakeIO:
    """假法官：一律往最重里判（一审冷却、再审 / 二审删除），把卷宗里编了号的 [本人→机器人] 全引用上"""

    def __init__(self):
        self.notes, self.logs, self.order, self.tasks, self.told, self.calls = [], [], [], [], [], []
        self.friend = True
        self.tell_ok = True  # 私聊通知发不发得出去
        self.admins, self.white, self.ai = {ADMIN}, {WHITE}, {AI}
        self.gate = None  # 一审的法官先卡住（占位期间）

    async def judge(self, system, text, timeout, meta):
        stage = "再审" if "（再审）" in system else ("二审" if "（二审）" in system else "一审")
        self.calls.append((stage, text))
        meta["model"] = "假法官"
        if self.gate is not None and stage != "再审":
            await self.gate.wait()
        part = text.split("【冷却期间他的话】")[-1] if stage == "再审" else text
        ids = [int(x) for x in re.findall(r"^\[#(\d+)\][^\n]*\[本人→机器人\]", part, re.M)]
        heavy = {"一审": "冷却", "再审": "删除", "二审": "删除"}[stage]
        return json.dumps({"verdict": heavy, "cites": ids, "kind": "辱骂找茬", "reason": "冲机器人骂人"}, ensure_ascii=False)

    async def notify(self, text):
        self.notes.append(text)
        return True

    async def tell(self, uid, text):
        if not self.tell_ok:
            return False
        self.told.append((uid, text))
        return True

    async def is_friend(self, uid):
        return self.friend

    async def delete_friend(self, uid):
        self.order.append("删好友")
        return True, ""

    async def wipe(self, uid):
        return "清了"

    def is_ai(self, uid):
        return uid in self.ai

    def is_admin(self, uid):
        return uid in self.admins

    def protected(self, uid):
        return "在管理员的白名单里" if uid in self.white else ""

    def spawn(self, coro):
        t = asyncio.get_running_loop().create_task(coro)
        self.tasks.append(t)
        return t

    def log(self, text):
        self.logs.append(text)


def mk(cfg=None, scfg=None, tell=True):
    """默认配置（只把「安静几秒再审」缩成 0、每天删除放宽，免得测试等 5 分钟）；时钟是假的：clk[0]"""
    dd = Path(tempfile.mkdtemp())
    clk = [NOON]
    st = bs.BlockStore(dd / "block.json", cfg=dict({"retrial_quiet_sec": 0, "delete_daily": 5}, **(scfg or {})), clock=lambda: clk[0])
    ml = bl.MsgLog(dd / "msgs.json", clock=lambda: clk[0])
    io = FakeIO()
    io.tell_ok = tell
    fl = bf.BlockCourt(st, ml, io, cfg=dict({"hold_sec": 30, "judge_timeout_sec": 2.0, "bridge_night": ""}, **(cfg or {})), clock=lambda: clk[0])
    fl.start_ts = 0
    return st, ml, io, fl, clk


async def settle(io, sec=5.0):
    end = time.time() + sec
    while time.time() < end:
        live = [t for t in io.tasks if not t.done()]
        if not live:
            return True
        await asyncio.wait(live, timeout=max(0.05, end - time.time()))
    return False


def say(ml, uid, text, t, mid=None):
    ml.note(uid, f"人{uid[-2:]}", G, text, bj.TAG_OWN, mid=mid or f"{uid}-{text}", now=t)


def ev(fl, clk, uid, text, mid):
    """他冲机器人说了一句（门口收证据那一步）"""
    return fl.evidence(uid, {"t": clk[0], "mid": mid, "conv": G, "tag": bj.TAG_OWN, "text": text}, clk[0])


def rts(st, cid):
    return (st.case(cid) or {}).get("retrials") or []


def rt_calls(io):
    return [t for s, t in io.calls if s == "再审"]


def numbered(text):
    """再审卷宗里编了号（能引用）的那几句"""
    return re.findall(r"^\[#\d+\][^\n]*\] (.*)$", text.split("【冷却期间他的话】")[-1], re.M)


def expire(st, uid, cid, clk, at):
    """冷却到点（照 tests/test_court.py 的写法：把到点时间拨到 at）"""
    st.d["cool"][uid]["until"] = int(at)
    st._case(cid)["cool_until"] = int(at)
    st.d["cooled"][uid]["last"] = int(at)
    st.sweep(clk[0])


async def court_tests():
    # ═══════════════ ① 通知之后才算 ═══════════════
    print("[① 占位那几分钟、通知发到他手里之前说的话：只当背景，不能拿来加重]")
    # 路径一（复核记录附录）：没有前科，他骂一句被立案 → 占位里又骂一句（这时他什么通知都没收到）→ 一审冷却 → 之后一句没说
    st, ml, io, fl, clk = mk()
    io.gate = asyncio.Event()
    say(ml, A, "傻机器人", NOON - 20)
    r = fl.file(A, "某群友", "他骂我", G, now=NOON)
    await asyncio.sleep(0.05)
    clk[0] = NOON + 5
    got = ev(fl, clk, A, "回话啊 垃圾", "h1")
    check(got and st.gate(A) == "hold" and not io.told, "占位期间他接着说的那句：收着（这时他还没收到任何通知）", (got, st.gate(A), io.told))
    clk[0] = NOON + 60
    io.gate.set()
    await settle(io)
    c = st.case(r["case"])
    check(c["state"] == "冷却中" and len(io.told) == 1 and "暂停" in io.told[0][1], "一审判冷却、私聊通知了他", (c.get("state"), io.told))
    clk[0] = NOON + 400
    await settle(io, 2)
    check(
        not rt_calls(io) and not rts(st, r["case"]) and not st.is_blocked_forever(A) and st.gate(A) == "cool",
        "回退检查：占位里说的那句不会被当成「冷却期间他的话」拿去再审（以前：约 6 分钟从没前科到永久不理）",
        (len(rt_calls(io)), rts(st, r["case"]), st.is_blocked_forever(A)),
    )
    check(bool(((st.case(r["case"]) or {}).get("ev") or [{}])[0].get("pre")), "那句在案子里标着「判决前说的」", (st.case(r["case"]) or {}).get("ev"))
    # 通知之后他同一天又骂了两句 → 再审：卷宗里占位那句单列、不编号；能引用的只有通知之后的两句
    clk[0] = NOON + 500
    ev(fl, clk, A, "还装死 废物", "r1")
    clk[0] = NOON + 510
    ev(fl, clk, A, "蠢货", "r2")
    await settle(io)
    tx = (rt_calls(io) or [""])[-1]
    pre_part = tx.split("【判决前说的】")[-1].split("【冷却期间他的话】")[0] if "【判决前说的】" in tx else ""
    check(
        "回话啊 垃圾" in pre_part and not re.search(r"\[#\d+\]", pre_part),
        "回退检查：再审卷宗里占位那句单列在【判决前说的】下，不编号",
        tx[-600:],
    )
    check(numbered(tx) == ["还装死 废物", "蠢货"], "回退检查：能引用的只有通知发到他手里之后说的那两句", numbered(tx))
    x = (rts(st, r["case"]) or [{}])[-1]
    check(
        x.get("verdict") == "删除" and x.get("result") == "不动" and not st.is_blocked_forever(A) and st.gate(A) == "cool",
        "回退检查：法官判删除，但这几句都在同一天 → 只冷却（照旧冷却到点）",
        x,
    )
    check(
        "天" in str(x.get("why")) and "2" in str(x.get("why")) and any("案子" in g and "再审" in g and "天" in g for g in io.logs),
        "回退检查：记录和日志里写清差在哪（分布在几天、要几天）",
        (x.get("why"), [g for g in io.logs if "再审" in g][-1:]),
    )

    # 通知没发出去（不是好友 / 发送失败）→ 没有「通知之后」
    print("\n[① 通知没发出去 → 没有「通知之后」→ 判不到永久不理]")
    st, ml, io, fl, clk = mk(tell=False)
    say(ml, B, "傻机器人", NOON - DAY - 3600)  # 昨天骂的
    r = fl.file(B, "某群友", "他骂我", G, now=NOON)
    await settle(io)
    c = st.case(r["case"])
    check(c["state"] == "冷却中" and not io.told and not fl._told_case(r["case"]), "一审判冷却，通知没发出去", (c.get("state"), io.told))
    clk[0] = NOON + 300
    ev(fl, clk, B, "装死是吧 废物", "n1")
    clk[0] = NOON + 310
    ev(fl, clk, B, "蠢货", "n2")
    await settle(io, 2)
    check(
        not rt_calls(io) and not st.is_blocked_forever(B) and st.gate(B) == "cool",
        "回退检查：他不知道自己被冷却时说的话不拿去再审、不会被永久不理（以前：昨天 1 句 + 今天 2 句 → 永久不理）",
        (len(rt_calls(io)), st.is_blocked_forever(B)),
    )
    # 他来私聊机器人 → 补上通知（发得出去了）→ 从这以后说的才算
    io.tell_ok = True
    clk[0] = NOON + 600
    ok = await fl.block_notice(B, clk[0])
    check(ok and len(io.told) == 1 and st.gate(B) == "cool", "他来私聊机器人时补上了通知", io.told)
    clk[0] = NOON + 700
    ev(fl, clk, B, "还是废物", "n3")
    clk[0] = NOON + 710
    ev(fl, clk, B, "等着 天天来骂", "n4")
    await settle(io)
    tx = (rt_calls(io) or [""])[-1]
    check(
        numbered(tx) == ["还是废物", "等着 天天来骂"] and "装死是吧 废物" in tx.split("【冷却期间他的话】")[0],
        "回退检查：补上通知以后说的两句才编号；之前那两句单列成背景",
        numbered(tx),
    )
    check(
        st.is_blocked_forever(B) and (rts(st, r["case"]) or [{}])[-1].get("result") == "删除",
        "通知之后还继续、前后一共 3 句、分布在 2 天 → 永久不理（门槛够了照旧执行）",
        rts(st, r["case"]),
    )
    # 没有通知渠道（外壳没给 tell）/ 关了「判完私聊告诉他」
    for label, kw, kill in (("没有通知渠道", {}, True), ("关了「判完私聊告诉他」", {"cfg": {"tell_user": False}}, False)):
        st, ml, io, fl, clk = mk(**kw)
        if kill:
            io.tell = None
        say(ml, C, "傻机器人", NOON - DAY - 3600)
        r = fl.file(C, "某群友", "他骂我", G, now=NOON)
        await settle(io)
        clk[0] = NOON + 300
        ev(fl, clk, C, "废物", "k1")
        clk[0] = NOON + 310
        ev(fl, clk, C, "蠢货", "k2")
        await settle(io, 2)
        check(
            st.gate(C) == "cool" and not st.is_blocked_forever(C) and not rt_calls(io),
            f"回退检查：{label} → 只冷却，不会被永久不理",
            (st.gate(C), len(rt_calls(io))),
        )

    # ═══════════════ ② 句数 / 天数门槛（再审）═══════════════
    print("\n[② 永久不理另加门槛：前后一共至少 3 句、分布在至少 2 天（再审）]")
    # 同一天：立案时 1 句 + 通知之后 2 句 = 3 句，但只有 1 天
    st, ml, io, fl, clk = mk()
    say(ml, D, "傻机器人", NOON - 20)
    r = fl.file(D, "某群友", "他骂我", G, now=NOON)
    await settle(io)
    clk[0] = NOON + 300
    ev(fl, clk, D, "废物", "d1")
    clk[0] = NOON + 310
    ev(fl, clk, D, "蠢货", "d2")
    await settle(io)
    x = (rts(st, r["case"]) or [{}])[-1]
    check(
        x.get("verdict") == "删除" and x.get("result") == "不动" and not st.is_blocked_forever(D) and "天" in str(x.get("why")),
        "回退检查：同一天 3 句（立案 1 + 通知之后 2）→ 法官判删除也只冷却，差的是天数",
        x,
    )
    # 两天、但一共只有 2 句
    st, ml, io, fl, clk = mk()
    say(ml, E, "傻机器人", NOON - DAY - 3600)
    r = fl.file(E, "某群友", "他骂我", G, now=NOON)
    await settle(io)
    clk[0] = NOON + 300
    ev(fl, clk, E, "废物", "e1")
    await settle(io)
    x = (rts(st, r["case"]) or [{}])[-1]
    check(
        x.get("verdict") == "删除" and x.get("result") == "不动" and not st.is_blocked_forever(E)
        and "句" in str(x.get("why")) and "3" in str(x.get("why")),
        "回退检查：分布在 2 天但前后一共只有 2 句 → 只冷却，差的是句数",
        x,
    )
    # 够了：昨天 1 句（立案时核实）+ 今天通知之后 2 句
    st, ml, io, fl, clk = mk()
    say(ml, F, "傻机器人", NOON - DAY - 3600)
    r = fl.file(F, "某群友", "他骂我", G, now=NOON)
    await settle(io)
    clk[0] = NOON + 300
    ev(fl, clk, F, "废物", "f1")
    clk[0] = NOON + 310
    ev(fl, clk, F, "蠢货", "f2")
    await settle(io)
    check(
        st.is_blocked_forever(F) and st.case(r["case"])["result"] == "删除" and any("永久不理" in t for _, t in io.told),
        "3 句、2 天、都在通知之后 → 永久不理，并通知他（没有多拦）",
        (rts(st, r["case"]), io.told[-1:]),
    )
    # 配置项：管理员把门槛调低（2 句、1 天）→ 同一天 1 + 1 也删
    st, ml, io, fl, clk = mk(scfg={"delete_min_total": 2, "delete_min_days": 1})
    say(ml, D, "傻机器人", NOON - 20)
    r = fl.file(D, "某群友", "他骂我", G, now=NOON)
    await settle(io)
    clk[0] = NOON + 300
    ev(fl, clk, D, "废物", "d1")
    await settle(io)
    check(st.is_blocked_forever(D), "两个门槛是配置项：调成 2 句 / 1 天 → 照配置来", rts(st, r["case"]))

    # ═══════════════ ② 二审 ═══════════════
    print("\n[② 二审：同样的门槛（上次冷却通知过他、前后一共 3 句、2 天）]")

    async def first_cool(uid, t_first, tell=True, scfg=None):
        """先被一审冷却一次（那句话在 t_first 说的）、冷却到点 → 冷却过"""
        st, ml, io, fl, clk = mk(tell=tell, scfg=scfg)
        say(ml, uid, "傻机器人", t_first)
        r = fl.file(uid, "某群友", "他骂我", G, now=t_first + 30)
        await settle(io)
        expire(st, uid, r["case"], clk, NOON - 600)
        io.tell_ok = True
        return st, ml, io, fl, clk, r["case"]

    # 够：前天 1 句（上次核实过）+ 今天 2 句；上次冷却通知过他
    st, ml, io, fl, clk, c1 = await first_cool(A, NOON - 2 * DAY)
    say(ml, A, "又是你这个废物", NOON - 60)
    say(ml, A, "蠢货 说的都是屁话", NOON - 50)
    r2 = fl.file(A, "某群友", "他又骂我", G, now=NOON)
    await settle(io)
    c = st.case(r2["case"])
    check(r2.get("stage") == "二审" and c["result"] == "删除" and st.is_blocked_forever(A), "二审：上次通知过、前后 3 句、2 天 → 永久不理（没有多拦）", c)
    # 上次冷却的通知没发到他手里
    st, ml, io, fl, clk, c1 = await first_cool(B, NOON - 2 * DAY, tell=False)
    say(ml, B, "又是你这个废物", NOON - 60)
    say(ml, B, "蠢货 说的都是屁话", NOON - 50)
    r2 = fl.file(B, "某群友", "他又骂我", G, now=NOON)
    await settle(io)
    c = st.case(r2["case"])
    check(
        r2.get("stage") == "二审" and c.get("verdict") == "删除" and c["result"] == "冷却" and not st.is_blocked_forever(B)
        and "通知" in str(c.get("result_why")),
        "回退检查：上次冷却的通知没发到他手里 → 二审判删除也只冷却，记录里写着差的是通知",
        (c.get("result"), c.get("result_why")),
    )
    check(any("通知" in n and "降级" in n for n in io.notes), "回退检查：发给管理员的那条里写着为什么降成冷却", io.notes[-1:])
    # 同一天（冷却配得很短的那种）
    st, ml, io, fl, clk, c1 = await first_cool(C, NOON - 3600, scfg={"cool_hours": 0.1})
    say(ml, C, "又是你这个废物", NOON - 60)
    say(ml, C, "蠢货 说的都是屁话", NOON - 50)
    r2 = fl.file(C, "某群友", "他又骂我", G, now=NOON)
    await settle(io)
    c = st.case(r2["case"])
    check(
        r2.get("stage") == "二审" and c["result"] == "冷却" and not st.is_blocked_forever(C) and "天" in str(c.get("result_why")),
        "回退检查：二审，前后 3 句都在同一天 → 只冷却，差的是天数",
        (c.get("result"), c.get("result_why")),
    )
    # 这次 2 句（昨天、今天各 1 句），上次那个案子的记录已经没有了 → 一共 2 句
    st, ml, io, fl, clk = mk()
    st.mark_cooled(D, int(NOON - 2 * DAY), now=NOON - 3 * DAY)
    st.d["cooled"][D]["told"] = int(NOON - 3 * DAY)
    say(ml, D, "废物", NOON - DAY - 60)
    say(ml, D, "蠢货", NOON - 50)
    r2 = fl.file(D, "某群友", "他又骂我", G, now=NOON)
    await settle(io)
    c = st.case(r2["case"])
    check(
        r2.get("stage") == "二审" and c["result"] == "冷却" and not st.is_blocked_forever(D)
        and "句" in str(c.get("result_why")) and "通知" not in str(c.get("result_why")),
        "回退检查：二审，分布在 2 天但前后一共只有 2 句 → 只冷却，差的是句数",
        (c.get("result"), c.get("result_why")),
    )
    # 删数据不等于洗白：「冷却过」上记的「通知过他」删数据以后还在
    st, ml, io, fl, clk, c1 = await first_cool(E, NOON - 2 * DAY)
    told_before = bool((st.d["cooled"].get(E) or {}).get("told"))
    fl.forget(E)
    check(told_before and bool((st.d["cooled"].get(E) or {}).get("told")), "「冷却过」上记着通知过他；删数据以后这个记号还在（删数据不等于洗白）", st.d["cooled"].get(E))

    # ═══════════════ ② 封顶函数 / 核对引用 / 老案子 ═══════════════
    print("\n[② 封顶函数、核对引用、v0.3.0 判的老案子]")
    S = bs.BlockStore(Path(tempfile.mkdtemp()) / "v.json", clock=lambda: NOON)
    m_today = [{"t": NOON - 100, "text": "骂1", "n": 1}, {"t": NOON - 90, "text": "骂2", "n": 2}]
    m_two = [{"t": NOON - DAY - 100, "text": "骂1", "n": 1}, {"t": NOON - 90, "text": "骂2", "n": 2}]
    yest = [NOON - DAY - 500]
    a = safe(S.trial_caps, "再审", "删除", m_today, prior=1, prior_ts=yest, told=True)
    b = safe(S.trial_caps, "再审", "删除", m_today, prior=1, prior_ts=[NOON - 500], told=True)
    c_ = safe(S.trial_caps, "再审", "删除", m_today[:1], prior=1, prior_ts=yest, told=True)
    e_ = safe(S.trial_caps, "再审", "删除", m_today, prior=1, prior_ts=yest, told=False)
    check(a["result"] == "删除", "再审封顶：通知之后 2 句 + 立案时 1 句（昨天）→ 删除", a)
    check(b["result"] == "不动" and "天" in b["why"], "回退检查：再审封顶：3 句同一天 → 不动", b)
    check(c_["result"] == "不动" and "句" in c_["why"], "回退检查：再审封顶：一共 2 句 → 不动", c_)
    check(e_["result"] == "不动" and "通知" in e_["why"], "回退检查：再审封顶：没通知到他 → 不动", e_)
    a = safe(S.trial_caps, "二审", "删除", m_today, prior=1, prior_ts=yest, told=True)
    b = safe(S.trial_caps, "二审", "删除", m_today, prior=0, prior_ts=[], told=True)
    c_ = safe(S.trial_caps, "二审", "删除", m_two, prior=1, prior_ts=yest)
    check(a["result"] == "删除", "二审封顶：这次 2 句 + 上次 1 句（昨天）、通知过 → 删除", a)
    check(b["result"] == "冷却" and "句" in b["why"], "回退检查：二审封顶：一共 2 句 → 保留 → 这次有核实过的 → 冷却", b)
    check(c_["result"] == "冷却" and "通知" in c_["why"], "回退检查：二审封顶：没说通知过没有 = 当没通知（拿不准判轻）→ 冷却", c_)
    MID = NOON - 12 * 3600  # 今天 0 点
    night = [{"t": MID + 180, "text": "骂2", "n": 1}, {"t": MID + 200, "text": "骂3", "n": 2}]  # 00:03
    a = safe(S.trial_caps, "再审", "删除", night, prior=1, prior_ts=[MID - 120], told=True)  # 立案那句 23:58
    b = safe(S.trial_caps, "再审", "删除", [dict(m, t=MID + 10 * 3600) for m in night], prior=1, prior_ts=[MID - 120], told=True)  # 第二天上午 10 点
    check(a["result"] == "不动" and "天" in a["why"], "回退检查：半夜 12 点前后连着说的（23:58 一句、00:03 两句）算同一天 → 不动", a)
    check(b["result"] == "删除", "头天夜里 1 句、第二天上午 2 句 → 算两天 → 删除", b)
    check(
        bs.DEFAULTS.get("delete_min_total") == 3 and bs.DEFAULTS.get("delete_min_days") == 2,
        "回退检查：两个门槛的代码默认值：3 句、2 天（老配置里没有这两项时也是这个数）",
        {k: bs.DEFAULTS.get(k) for k in ("delete_min_total", "delete_min_days")},
    )
    check(
        (BK.get("delete_min_total") or {}).get("default") == 3 and (BK.get("delete_min_days") or {}).get("default") == 2
        and all((BK.get(k) or {}).get("hint") and (BK.get(k) or {}).get("description") for k in ("delete_min_total", "delete_min_days")),
        "回退检查：配置页里有这两项（默认 3 / 2），带说明",
        {k: BK.get(k) for k in ("delete_min_total", "delete_min_days")},
    )
    check(
        "通知" in (BK.get("delete_min_total") or {}).get("hint", "") and "只冷却" in (BK.get("delete_min_total") or {}).get("hint", ""),
        "回退检查：说明里写清：通知之后说的才算、不够的只冷却",
        (BK.get("delete_min_total") or {}).get("hint"),
    )
    tb = {1: {"t": NOON - 50, "mid": "p", "uid": A, "conv": G, "tag": bj.TAG_OWN, "text": "判决前说的", "pre": True},
          2: {"t": NOON - 40, "mid": "q", "uid": A, "conv": G, "tag": bj.TAG_OWN, "text": "通知之后说的"}}
    okv = S.verify([1, 2], tb, A, NOON)
    check([m["n"] for m in okv] == [2], "回退检查：核对引用：标着「判决前说的」的那句就算被引用了也不算数", [m["n"] for m in okv])
    # v0.3.0 判的、还在冷却中的老案子：案子上没有通知时间，只有「告诉过他」那一行
    st, ml, io, fl, clk = mk(tell=False)
    say(ml, A, "傻机器人", NOON - DAY - 3600)
    r = fl.file(A, "某群友", "他骂我", G, now=NOON)
    await settle(io)
    clk[0] = NOON + 100
    st.mark_told(f"case:{r['case']}:冷却", clk[0])  # 老版本只记了这一行
    clk[0] = NOON + 100.5
    ev(fl, clk, A, "同一秒里说的", "o1")
    clk[0] = NOON + 200
    ev(fl, clk, A, "废物", "o2")
    clk[0] = NOON + 210
    ev(fl, clk, A, "蠢货", "o3")
    await settle(io)
    tx = (rt_calls(io) or [""])[-1]
    check(numbered(tx) == ["废物", "蠢货"], "回退检查：老案子按「告诉过他」那一行的时间分（同一秒里说的算通知之前）", numbered(tx))

    # v0.3.0 收的证据上没有「判决前说的」记号（升级时正在冷却的案子）：照通知时间补
    st, ml, io, fl, clk = mk()
    io.gate = asyncio.Event()
    say(ml, B, "傻机器人", NOON - DAY - 3600)
    r = fl.file(B, "某群友", "他骂我", G, now=NOON)
    await asyncio.sleep(0.05)
    clk[0] = NOON + 5
    ev(fl, clk, B, "占位里说的", "g1")
    clk[0] = NOON + 60
    io.gate.set()
    await settle(io)
    cc = st._case(r["case"])
    cc.pop("told_t", None)  # 老版本的存档：案子上没有通知时间、证据上没有记号，只有「告诉过他」那一行
    for e_ in cc.get("ev") or []:
        e_.pop("pre", None)
    n0 = len(rt_calls(io))
    fl._rt_kick(r["case"])
    await settle(io, 2)
    check(len(rt_calls(io)) == n0 and not rts(st, r["case"]), "回退检查：老存档里占位那句（没有记号）也不为它开再审", (len(rt_calls(io)) - n0, rts(st, r["case"])))
    clk[0] = NOON + 300
    ev(fl, clk, B, "废物", "g2")
    clk[0] = NOON + 310
    ev(fl, clk, B, "蠢货", "g3")
    await settle(io)
    tx = (rt_calls(io) or [""])[-1]
    check(numbered(tx) == ["废物", "蠢货"] and "占位里说的" in tx.split("【冷却期间他的话】")[0], "回退检查：老存档里占位那句进了再审卷宗也不编号", numbered(tx))
    # 老存档的「冷却过」上没有「通知过他」的记号：按「告诉过他」那一行认
    st, ml, io, fl, clk, c1 = await first_cool(D, NOON - 2 * DAY)
    st.d["cooled"][D].pop("told", None)
    st._case(c1).pop("told_t", None)
    say(ml, D, "又是你这个废物", NOON - 60)
    say(ml, D, "蠢货 说的都是屁话", NOON - 50)
    r2 = fl.file(D, "某群友", "他又骂我", G, now=NOON)
    await settle(io)
    check(st.case(r2["case"])["result"] == "删除", "老存档的「冷却过」：照「告诉过他」那一行算通知过 → 二审门槛够了照旧删除", st.case(r2["case"]).get("result_why"))

    # ═══════════════ ③ 关掉二审 ═══════════════
    print("\n[③ 关掉「二审」= 再审也判不到永久不理]")
    st, ml, io, fl, clk = mk(cfg={"second": False})
    say(ml, F, "傻机器人", NOON - DAY - 3600)
    r = fl.file(F, "某群友", "他骂我", G, now=NOON)
    await settle(io)
    clk[0] = NOON + 300
    ev(fl, clk, F, "废物", "f1")
    clk[0] = NOON + 310
    ev(fl, clk, F, "蠢货", "f2")
    await settle(io)
    x = (rts(st, r["case"]) or [{}])[-1]
    check(
        st.gate(F) == "cool" and not st.is_blocked_forever(F) and (not x or (x.get("result") == "不动" and "二审" in str(x.get("why")))),
        "回退检查：二审关着：3 句、2 天、通知之后都够，法官判删除 → 也只冷却（配置说明写的「永远不会被判删除」）",
        (st.gate(F), x),
    )
    h = BK["second"]["hint"]
    check("再审" in h and "永远不会被判删除" in h, "回退检查：「二审」的说明写明：关了以后冷却中的再审也不会判删除", h)

    # ═══════════════ 提示词 / 自检 ═══════════════
    print("\n[提示词、自检]")
    PR = bj.sys_prompt("再审")
    check("【判决前说的】" in PR and "不能引用" in PR and "通知" in PR, "回退检查：再审的提示词写明【判决前说的】只是背景、不能引用", PR[:400])
    bad_words = ("你应该", "你要说", "可以说", "要道歉", "语气", "撒娇", "比如说", "例如：", "回复他", "就说", "别怪", "别说", "你可以")
    check(not any(w in PR for w in bad_words), "再审的提示词还是只写判断规则（没有台词、语气）", [w for w in bad_words if w in PR])
    from core import selftest as stt  # noqa: E402

    async def heavy(system, text):
        stage = "再审" if "（再审）" in system else ("二审" if "（二审）" in system else "一审")
        part = text.split("【冷却期间他的话】")[-1] if stage == "再审" else text
        ids = [int(x) for x in re.findall(r"^\[#(\d+)\][^\n]*\[本人→机器人\]", part, re.M)]
        return json.dumps({"verdict": {"一审": "冷却", "再审": "删除", "二审": "删除"}[stage], "cites": ids, "kind": "其他", "reason": "x"}, ensure_ascii=False)

    out = await stt.run(d / "data" / "court_cases.json", heavy, scenarios=[11, 16])
    fin = {r_["sc"]: r_["final"] for r_ in out["results"]}
    check(fin == {11: "删除", 16: "删除"}, "自检只看法官判得对不对：情景里没有日期和通知，不拿天数 / 通知的门槛卡它", fin)


asyncio.run(court_tests())

# ═══════════════ ④ 纠错庭：判词只认固定的三个写法 ═══════════════
print("\n[④ 纠错庭：判词只认「机器人错了 / 机器人没错 / 说不准」]")


def vd(s, real=True):
    v = ct.parse_screen(json.dumps({"real": real, "checkable": True, "evidence": 1, "verdict": s, "right": "对的说法"}, ensure_ascii=False))
    return None if v is None else v["verdict"]


check(
    (vd("机器人错了"), vd("机器人没错"), vd("说不准")) == ("错了", "没错", "说不准"),
    "三个固定写法照旧：机器人错了 → 错了、机器人没错 → 没错、说不准 → 说不准",
    (vd("机器人错了"), vd("机器人没错"), vd("说不准")),
)
check(
    (vd(" 机器人错了。"), vd("「机器人没错」"), vd("说不准\n")) == ("错了", "没错", "说不准"),
    "前后带空格、句号、引号的也认（字还是那三个）",
    (vd(" 机器人错了。"), vd("「机器人没错」"), vd("说不准\n")),
)
for s in ("机器人没说错", "没有说错", "他自己说错了", "用户错了", "说不准，可能是他记错了", "不确定谁错"):
    check(vd(s) != "错了", f"回退检查：判词写成「{s}」→ 不再读成「机器人错了」", vd(s))
for s in ("他说得不错", "他纠正得不错，机器人错了"):
    check(vd(s) != "没错", f"回退检查：判词写成「{s}」→ 不再读成「机器人没错」", vd(s))
odd = ("机器人没说错", "说不准，可能是他记错了", "他说得不错", "机器人错了/机器人没错/说不准", "不好说", "", "错了吧")
check(
    all(vd(s) is None for s in odd),
    "回退检查：他真在纠正、判词却不是那三个写法（或者没写）→ 没判成（不猜）",
    {s: vd(s) for s in odd},
)
v = ct.parse_screen('{"real": false, "checkable": false, "evidence": 0}')
check(v is not None and v["real"] is False and v["verdict"] == "说不准", "判断模型说「不算纠正」的：没写判词也照常读（本来就不认错、不记教训）", v)
v = ct.parse_screen('{"real": false, "verdict": "他瞎说的 机器人错了才怪"}')
check(v is not None and v["real"] is False and v["verdict"] != "错了", "回退检查：「不算纠正」的判词里带「错」字也不读成机器人错了", v)
check("机器人错了" in ct.SCREEN and "机器人没错" in ct.SCREEN and "说不准" in ct.SCREEN and "原样" in ct.SCREEN,
      "回退检查：判断规则里写明 verdict 只能原样写三个里的一个", ct.SCREEN[-420:])
st_txt = ct.status_text({"name": "小李", "her": "二战是 1944 年结束的", "his": "不对"}, None)
check(st_txt == "", "没判成 → 这一轮不给模型任何核对结果")

# ═══════════════ ⑤ 删教训：按 QQ 号，不靠名片上的字 ═══════════════
print("\n[⑤ 删教训：自助的只按 QQ 号；按名片连带的不看固定格式的字]")
T = 1_800_000_000.0
LG = "aiocqhttp:GroupMessage:7001"


def lessons3():
    L = ls.LessonStore(Path(tempfile.mkdtemp()) / "l.json", clock=lambda: T)
    for i, (uid, name, wrong, right, topic, prompt) in enumerate((
        ("30001", "小李", "二战 1944 年结束", "1945 年", "二战 结束", "二战哪年结束"),
        ("30002", "老王", "珠峰 8000 米", "8848 米", "珠峰 高度", "珠峰多高"),
        ("30003", "阿青", "一年 360 天", "365 天", "一年 天数", "一年几天"),
    )):
        L.add_fact({"id": f"c{i}", "conv": LG, "uid": uid, "name": name, "her": wrong, "prompt": prompt},
                   {"real": True, "verdict": "错了", "wrong": wrong, "right": right, "topic": topic})
    L.add_format(LG, "50005", "验证程序", {"kind": "digits", "desc": "只发一个数字"}, "pass")
    return L


L = lessons3()
n = L.wipe_user("39999", names=["核对过"])
check(n == 0 and len(L.lessons) == 4, "回退检查：名片叫「核对过」的人删数据 → 别人的事实教训一条都不动（以前：全删光）", (n, len(L.lessons)))
L = lessons3()
n = L.wipe_user("39999", names=["指出"])
check(n == 0 and len(L.lessons) == 4, "回退检查：名片叫「指出」也一样", (n, len(L.lessons)))
L = lessons3()
n = L.wipe_user("39999", names=["验证程序"])
check(n == 0 and len(L.lessons) == 4, "回退检查：名片叫「验证程序」→ 别人的格式教训不动", (n, len(L.lessons)))
L = lessons3()
n = L.wipe_user("39999", names=["格式不对"])
check(n == 0 and len(L.lessons) == 4, "回退检查：名片叫「格式不对」→ 格式教训里的固定说明不算提到他", (n, len(L.lessons)))
L = lessons3()
n = L.wipe_user("30001", names=["小李"])
check(n == 1 and all(x["uid"] != "30001" for x in L.lessons), "他自己指出的教训：按 QQ 号删（照旧）", n)
L = lessons3()
n = L.wipe_user("50005", names=[])
check(n == 1 and all(x.get("kind") != "格式" for x in L.lessons), "回他那个号的格式教训：按 QQ 号删（照旧）", n)
L = lessons3()
L.add_fact({"id": "c9", "conv": LG, "uid": "30002", "name": "老王", "her": "小李今年 18 岁", "prompt": "小李多大"},
           {"real": True, "verdict": "错了", "wrong": "小李 18 岁", "right": "小李 20 岁", "topic": "小李 年龄"})
n = L.wipe_user("30001", names=["小李"])
check(n == 2, "管理员删某个人：别人指出、内容里提到他名字的教训也删（照旧）", n)

# ═══════════════ ⑥ main.py 这一层（假的 astrbot 包）═══════════════
print("\n[⑥ main.py 这一层（假的 astrbot 包顶上，不是真 AstrBot）]")


class Log:
    def __init__(self):
        self.lines = []

    def info(self, m):
        self.lines.append(str(m))

    warning = debug = error = info


class _Group:
    def command(self, *a, **k):
        return lambda f: f

    def group(self, *a, **k):
        return lambda f: _Group()


class _Filter:
    class CustomFilter:
        pass

    class EventMessageType:
        ALL = "all"

    class PermissionType:
        ADMIN = "admin"

    @staticmethod
    def command_group(*a, **k):
        return lambda f: _Group()

    def __getattr__(self, name):
        return lambda *a, **k: (lambda f: f)


class _Comp:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class Plain(_Comp):
    pass


class TextPart:
    def __init__(self, text=""):
        self.text = text

    def mark_as_temp(self):
        return self


class Star:
    def __init__(self, context=None):
        self.context = context


LOG = Log()
mods = {}
for name in ("astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.provider", "astrbot.api.star", "astrbot.api.message_components",
             "astrbot.core", "astrbot.core.agent", "astrbot.core.agent.message"):
    m = types.ModuleType(name)
    m.__path__ = []
    mods[name] = sys.modules[name] = m
mods["astrbot.api"].logger = LOG
mods["astrbot.api"].AstrBotConfig = dict
mods["astrbot.api"].message_components = mods["astrbot.api.message_components"]
ev_mod = mods["astrbot.api.event"]
ev_mod.AstrMessageEvent, ev_mod.MessageEventResult = object, object
ev_mod.MessageChain = lambda chain=None: list(chain or [])
ev_mod.filter = _Filter()
mods["astrbot.api.provider"].ProviderRequest = object
mods["astrbot.api.star"].Context = object
mods["astrbot.api.star"].Star = Star
mods["astrbot.api.star"].StarTools = types.SimpleNamespace(get_data_dir=lambda name: Path(tempfile.mkdtemp()))
cm = mods["astrbot.api.message_components"]
cm.Plain = Plain
for nm in ("Image", "At", "Reply", "Forward", "Node", "Nodes"):
    setattr(cm, nm, type(nm, (_Comp,), {}))
mods["astrbot.core.agent.message"].TextPart = TextPart
pkg = types.ModuleType("court_plugin")
pkg.__path__ = [str(d)]
sys.modules["court_plugin"] = pkg


class Platform:
    def meta(self):
        return types.SimpleNamespace(id="qq1", name="aiocqhttp")


class Ctx:
    def __init__(self):
        self.send = True  # send_message 的结果：True 发了 / False 没有这个平台 / 异常
        self.sent = []
        self.reply = ""
        self.platform_manager = types.SimpleNamespace(platform_insts=[Platform()])

    def get_config(self, umo=None):
        return {"admins_id": ["10009"]}

    def get_registered_star(self, name):
        return None

    def register_web_api(self, *a, **k):
        pass

    async def send_message(self, session, chain):
        if isinstance(self.send, Exception):
            raise self.send
        if self.send:
            self.sent.append(session)
        return self.send

    async def get_current_chat_provider_id(self, umo):
        return "fake"

    async def llm_generate(self, chat_provider_id=None, prompt=None, system_prompt=None):
        return types.SimpleNamespace(completion_text=self.reply)


class FakeEvent:
    def __init__(self, uid):
        self.uid = uid

    def get_sender_id(self):
        return self.uid

    def plain_result(self, text):
        return text


try:
    MAINMOD = importlib.import_module("court_plugin.main")
    err = ""
except Exception as e:  # noqa: BLE001
    MAINMOD, err = None, repr(e)
check(MAINMOD is not None, "main.py 在假的 astrbot 包上能导入", err)
META = (d / "metadata.yaml").read_text(encoding="utf-8")
check(MAINMOD is not None and MAINMOD.VERSION == "0.3.1" and "version: v0.3.1" in META, "回退检查：版本号 0.3.1（main.py 和 metadata.yaml 一样）",
      (getattr(MAINMOD, "VERSION", None), re.findall(r"version: .*", META)))
check(
    "from astrbot.api import AstrBotConfig, logger" in (d / "main.py").read_text(encoding="utf-8")
    and not any(re.search(r"^\s*import logging|getLogger", (p).read_text(encoding="utf-8"), re.M) for p in [d / "main.py", *sorted((d / "core").glob("*.py"))]),
    "日志只用 astrbot.api 的 logger",
)
badf = [str(p.relative_to(d)) for p in [*d.glob("*.py"), *d.glob("*.md"), *d.glob("*.json"), *d.glob("*.yaml"), *(d / "core").glob("*.py"), *(d / "tests").glob("*.py")]
        if b"\x00" in p.read_bytes() or p.read_bytes().startswith(b"\xef\xbb\xbf")]
check(not badf, "文件里没有 NUL / BOM", badf)


async def main_tests():
    ctx = Ctx()
    star = MAINMOD.FatfishCourt(ctx, {"block": {"delete_min_total": 4, "delete_min_days": 3}})
    star._refresh_cfg()
    check(star.store.cfg.get("delete_min_total") == 4 and star.store.cfg.get("delete_min_days") == 3,
          "回退检查：配置里填的两个门槛接到了拉黑庭上", {k: star.store.cfg.get(k) for k in ("delete_min_total", "delete_min_days")})
    star2 = MAINMOD.FatfishCourt(Ctx(), {})
    star2._refresh_cfg()
    check(star2.store.cfg.get("delete_min_total") == 3 and star2.store.cfg.get("delete_min_days") == 2,
          "回退检查：老配置里没有这两项 → 用代码默认值 3 / 2", {k: star2.store.cfg.get(k) for k in ("delete_min_total", "delete_min_days")})
    # /法庭、/法庭 案子：现在的门槛、降成冷却的原因
    cid = star2.store.open_case("10002", "某群友", "他骂我", LG, cool_until=time.time() + 600, evok=True)
    star2.store.update_case(cid, state="冷却中", verdict="冷却", result="冷却", n_ok=1)
    star2.store.retrial_end(cid, {"t": int(time.time()), "k": 1, "verdict": "删除", "n_ok": 2, "result": "不动",
                                  "why": "法官判删除，但永久不理的门槛没到：这些话分布在 1 天（要至少 2 天） → 只冷却"})
    txt, stx = star2.cases_text(), star2.court_status_text()
    check("不动（法官判删除，但永久不理的门槛没到：这些话分布在 1 天（要至少 2 天）" in txt, "回退检查：「/法庭 案子」里写着法官判了删除为什么只冷却", txt)
    check("≥3 句" in stx and "≥2 天" in stx and "通知" in stx, "回退检查：「/法庭」状态里写着现在判删除的门槛", stx)
    # 通知发没发出去
    ok1 = await star._tell_user("10001", "（系统消息）x")
    ctx.send = False
    ok2 = await star._tell_user("10001", "（系统消息）x")
    ctx.send = RuntimeError("发送失败")
    ok3 = await star._tell_user("10001", "（系统消息）x")
    check(ok1 is True and ok3 is False, "私聊通知：发出去了 → 算；发送报错 → 不算（照旧）", (ok1, ok3))
    check(ok2 is False, "回退检查：私聊通知：AstrBot 说没发（找不到这个平台）→ 不算发到他手里", ok2)
    # 判词读不出 → 没判成：不认错、不记教训、不给状态、记日志
    ctx.send = True
    case = {"id": "t1", "conv": LG, "uid": "30001", "name": "小李", "private": False, "her": "二战是 1945 年结束的", "his": "你说错了吧", "prompt": "二战哪年结束"}
    star.court.log.append(dict(case))
    for i, word in enumerate(("说不准，可能是他记错了", "机器人没说错")):
        ctx.reply = json.dumps({"real": True, "checkable": True, "evidence": 0, "verdict": word, "right": ""}, ensure_ascii=False)
        n_log = len(LOG.lines)
        v = await star._screen(dict(case, id=f"t{i}"), [], LG)
        st_text, _ = star.court.take_status(LG, "30001")
        check(
            v is None and not star.lessons.lessons and st_text == "" and star._today().get("wrong", 0) == 0,
            f"回退检查：判断模型写「{word}」→ 没判成：不认错、不记教训、这一轮不给核对结果（以前：告诉机器人「你那句确实不对」并记成教训）",
            (v, star.lessons.lessons, st_text),
        )
        check(any("没给能用的结论" in x for x in LOG.lines[n_log:]), "　　日志里记了一笔", LOG.lines[n_log:])
        star.court.res.clear()
    ctx.reply = json.dumps({"real": True, "checkable": True, "evidence": 2, "verdict": "机器人错了", "wrong": "1944 年", "right": "1945 年", "topic": "二战 结束"}, ensure_ascii=False)
    v = await star._screen(dict(case, id="t9", her="二战是 1944 年结束的"), [], LG)
    check(v and v["verdict"] == "错了" and len(star.lessons.lessons) == 1, "判词照格式写「机器人错了」→ 照旧认、记教训", v)
    # 忘记我：只按 QQ 号
    star.lessons.lessons.clear()
    for x in lessons3().lessons:
        star.lessons.lessons.append(x)
    star.names["39999"] = {"核对过"}
    star.names["30001"] = {"小李"}
    r_ = safe(star.forget_user, "39999", by_name=False)
    check(r_.get("lessons") == 0 and len(star.lessons.lessons) == 4, "回退检查：名片改成「核对过」发「忘记我」→ 一条教训都没少", (r_, len(star.lessons.lessons)))
    for label, fn in (("/纠错 忘记我", "cmd_forget_me"), ("/法庭 忘记我", "court_forget_me")):
        star.names["39999"] = {"核对过"}
        outs = [x async for x in getattr(star, fn)(FakeEvent("39999"))]
        check(len(star.lessons.lessons) == 4 and outs and "0 条" in str(outs[0]),
              f"回退检查：名片改成「核对过」的人发「{label}」→ 别人的教训一条都没少", (len(star.lessons.lessons), outs))
        if len(star.lessons.lessons) != 4:
            star.lessons.lessons[:] = lessons3().lessons
    star.lessons.lessons.append({"id": "zz", "kind": "事实", "uid": "30002", "uids": ["30002"], "name": "老王", "when": "小李多大", "wrong": "小李 18 岁",
                                 "right": "小李 20 岁", "basis": "", "topic": "小李 年龄", "src": "老王指出、核对过", "conv": LG, "keys": []})
    r1 = safe(star.forget_user, "30001", by_name=False)
    check(r1.get("lessons") == 1 and any(x["id"] == "zz" for x in star.lessons.lessons), "忘记我：自己指出的那条删了；别人指出、只是提到他名字的那条留着（管理员能删）", r1)
    star.names["30001"] = {"小李"}
    r2 = star.forget_user("30001")
    check(r2.get("lessons") == 1 and not any(x["id"] == "zz" for x in star.lessons.lessons), "管理员「忘记 <QQ>」：提到他名字的那条也删（照旧）", r2)
    await star.terminate()
    await star2.terminate()


if MAINMOD is not None:
    asyncio.run(main_tests())

ok = sum(1 for c_, _ in RES if c_)
bad = [m_ for c_, m_ in RES if not c_]
print("\n" + "=" * 60)
print(f"  结果：{ok} 项过了" + (f"，{len(bad)} 项没过" if bad else "，全过"))
for m_ in bad:
    print("    -", m_)
print("=" * 60)
sys.exit(1 if bad else 0)
