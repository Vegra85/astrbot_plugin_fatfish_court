#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拉黑庭 v0.2.0 的单元测试（不用装 AstrBot，几秒钟）：python tests/test_court_020.py [插件目录 默认上一层]
照作者自己的 QQ 机器人 11-07 ~ 11-08（v1.17.26–30）：
  ① 从轻：算数的只剩红线；暧昧、擦边梗、往床上的玩笑不算；三种审都「拿不准判轻的」；判词种类擦边 → 色情
  ② 卷宗：机器人回他的话穿插进去（不编号、不能引用）；图的描述（有的话）；系统故障一节（外壳给了才有）；早的带擦边图的优先留着
  ③ 记机器人回他的话（block_log.note_bot / bot_for / 过期 / 删数据）；拼卷宗时带上
  ④ 法官没判成 → 从轻放过（fail_release；关掉 = 以前的只冷却）
  ⑤ 告诉被告：判了冷却 / 删除 → 私聊一条系统消息（法庭口吻、原因只一个词、不发庭审意见）；放过不发；同一个案子同一个结果只说一次；
     真删好友之前先发、再删；再审判删除也发；没告诉过的人来私聊时补上；没案子的冷却说一句「暂停中」
  ⑥ 衔接：冷却到点 → 告诉他恢复了（找过几次、会进二审的加一句；没告诉过他在冷却的不说；夜里到点的先记着、他先来找就先说）；
     管理员撤销 → 告诉他（申诉过的写「看过你的申诉」）
  ⑦ 冷却 / 永久不理期间他找过几次（只记次数）；申诉回执；管理员立案默认关；配置
  ⑧ 冷却到点他一来就先发那条（不等定时）；开机补告诉（冷却中、结果还没告诉过他的）；又申诉 → 回一句「之前的已经转了」
拿 v0.1.0 的代码跑：一跑就失败（没有这些函数）"""

import asyncio
import json
import re
import sys
import tempfile
import time
from pathlib import Path

d = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(d))
from core import block_flow as bf  # noqa: E402
from core import block_judge as bj  # noqa: E402
from core import block_log as bl  # noqa: E402
from core import block_store as bs  # noqa: E402

RES = []


def check(c, m, why=""):
    RES.append((bool(c), m))
    print(("  [过]   " if c else "  [没过] ") + m + ("" if c or not why else f"\n           {str(why)[:700]}"))


G = "aiocqhttp:GroupMessage:7501"
P = "aiocqhttp:FriendMessage:10001"
A, B, C, D, E, ADMIN, WHITE, AI = "10001", "10002", "10003", "10004", "10005", "10009", "10007", "50005"
HEAVY = {"一审": "冷却", "再审": "删除", "二审": "删除"}
LIGHT = {"一审": "放过", "再审": "不动", "二审": "保留"}


class FakeIO:
    def __init__(self, tell=True):
        self.notes, self.logs, self.order, self.tasks, self.told, self.calls = [], [], [], [], [], []
        self.friend = True
        self.admins, self.white, self.ai = {ADMIN}, {WHITE}, {AI}
        self.mode = "smart"
        if not tell:
            self.tell = None

    async def judge(self, system, text, timeout, meta):
        stage = "再审" if "（再审）" in system else ("二审" if "（二审）" in system else "一审")
        self.calls.append((stage, text))
        meta["model"] = "假法官"
        if self.mode == "raise":
            raise RuntimeError("调不通")
        part = text.split("【冷却期间他的话】")[-1] if stage == "再审" else text
        bad = [int(x) for x in re.findall(r"\[#(\d+)\][^\n]*BAD", part)]
        if bad and self.mode == "smart":
            return json.dumps({"verdict": HEAVY[stage], "cites": bad, "kind": "辱骂找茬", "reason": "冲机器人骂人"}, ensure_ascii=False)
        return json.dumps({"verdict": LIGHT[stage], "cites": [], "kind": "无", "reason": "斗嘴而已"}, ensure_ascii=False)

    async def notify(self, text):
        self.notes.append(text)
        return True

    async def tell(self, uid, text):
        self.order.append("告诉他")
        self.told.append((uid, text))
        return True

    async def is_friend(self, uid):
        self.order.append("查好友")
        return self.friend

    async def delete_friend(self, uid):
        self.order.append("删好友")
        return True, ""

    async def wipe(self, uid):
        self.order.append("清数据")
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
    dd = Path(tempfile.mkdtemp())
    st = bs.BlockStore(dd / "block.json", cfg=dict({"retrial_quiet_sec": 0, "delete_daily": 5}, **(scfg or {})))
    ml = bl.MsgLog(dd / "msgs.json")
    io = FakeIO(tell)
    fl = bf.BlockCourt(st, ml, io, cfg=dict({"hold_sec": 30, "judge_timeout_sec": 2.0, "bridge_night": ""}, **(cfg or {})))
    fl.start_ts = 0
    return st, ml, io, fl, dd


async def settle(io, sec=5.0):
    end = time.time() + sec
    while time.time() < end:
        live = [t for t in io.tasks if not t.done()]
        if not live:
            return True
        await asyncio.wait(live, timeout=max(0.05, end - time.time()))
    return False


def say(ml, uid, text, tag=bj.TAG_OWN, conv=G, ago=600, mid=None):
    ml.note(uid, f"人{uid[-2:]}", conv, text, tag, mid=mid or f"{uid}-{len(text)}-{ago}", now=time.time() - ago)


def told(io, uid):
    return [t for u, t in io.told if u == uid]


def cooled_before(st, ml, uid):
    """他以前被冷却过、三天前到点，那次的通知发到了他手里；昨天又冲机器人骂了一句
    （v0.3.1：二审判删除要 上次通知过他 + 前后一共 3 句 + 分布在 2 天，见 tests/test_court_031.py —— 这里凑齐，下面测的是「告诉他」）"""
    st.mark_cooled(uid, int(time.time()) - 3 * 86400)
    st.d["cooled"][uid]["told"] = int(time.time()) - 4 * 86400
    say(ml, uid, "昨天也骂了 BAD", ago=90000)


# ═══════════════ ① 从轻：提示词 ═══════════════
print("\n[① 从轻：算数的只剩红线；三种审都拿不准判轻的]")
P1, PR, P2 = bj.sys_prompt("一审"), bj.sys_prompt("再审"), bj.sys_prompt("二审")
check("拿不准就放过" in P1 and "出现一次就算" not in P1 and "拿不准就判放过" in P1,
      "一审：拿不准就放过（回退检查：v0.1.0「一审不要求反复，出现一次就算」）", P1[:300])
check("拿不准就不动" in PR and "拿不准就判不动" in PR and "拿不准就保留" in P2 and "拿不准就判保留" in P2, "再审 / 二审：拿不准判轻的那个")
check(all(x in P1 for x in ("往床上、身体接触方向开的玩笑", "机器人自己先起的头", "系统故障期间", "熟不熟都一样")),
      "不算的：往床上的玩笑、机器人自己起的头、故障期间的追问（跟熟不熟无关）")
check(all(x in P1 for x in ("缩进、标「机器人」的那几行", "【系统故障】", "「[图片：…]」")), "判断规则：机器人的话怎么看、系统故障、图的描述")
pb = bj.parse_block('{"verdict":"冷却","cites":[1],"kind":"擦边","reason":"x"}', {1}, ("放过", "冷却"))
check("色情" in bj.BLOCK_KINDS and "擦边" not in bj.BLOCK_KINDS and "无视拒绝" in bj.BLOCK_KINDS and pb and pb["kind"] == "其他",
      "判词种类：擦边不再是一类（露骨的叫色情；多了无视拒绝、未成年）", pb)

# ═══════════════ ② 卷宗 ═══════════════
print("\n[② 卷宗：机器人的话穿插（不编号）、图的描述、系统故障、早的带擦边图的优先留]")
now = time.time()
t0 = now - 20 * 3600
msgs = [{"t": t0 + i * 3600, "mid": 100 + i, "uid": A, "conv": P, "where": "私聊", "tag": bj.TAG_OWN, "text": f"第{i}句 正常聊天"} for i in range(8)]
msgs[0].update(text="[图片]", imgd=["字：时不时进裙里看看；一只猫探头"], flag=True)
bots = [{"t": t0 + 7 * 3600 + 60, "conv": P, "where": "私聊", "text": "哈哈哈你别闹"},
        {"t": t0 + 30, "conv": P, "where": "私聊", "text": "这张就算了"},
        {"t": t0 + 3 * 3600 + 30, "conv": G, "where": "群 A", "text": "别的对话里的话"}]
case = {"name": "小甲", "msgs": msgs, "bot": bots, "limit": 5, "days": 7, "now": now}
text, table = bj.block_input(case)
check(len(table) == 5 and 100 in [m.get("mid") for m in table.values()] and 103 not in [m.get("mid") for m in table.values()],
      "早的、图里有擦边的 → 换进来（换掉最早的普通那句）", [m.get("mid") for m in table.values()])
check("[图片：字：时不时进裙里看看；一只猫探头]" in text, "「[图片]」换成图的描述", text[-800:])
hl = [ln for ln in text.splitlines() if ln.startswith("    机器人 ")]
check(len(hl) == 2 and any("哈哈哈你别闹" in x for x in hl) and any("这张就算了" in x for x in hl) and not any("别的对话" in x for x in hl),
      "机器人的话穿插进去：同一个对话、他那句前后的；别的对话的不放（回退检查：v0.1.0 卷宗里没有机器人的话）", hl)
check(re.findall(r"^\[#(\d+)\]", text, re.M) == [str(i) for i in range(1, 6)], "只给他的话编号；机器人的话不编号（不能引用）")
check(text.index("第7句") < text.index("哈哈哈你别闹"), "按时间排")
check("【系统故障】" not in text, "外壳没给故障记录（没有 outages 这个键）→ 不写这一节（插件看不到主模型的故障）")
t2, _ = bj.block_input(dict(case, outages=[{"s": t0 + 3600, "e": t0 + 3900, "err": "模型 500"}]))
t3, _ = bj.block_input(dict(case, outages=[]))
check("【系统故障】" in t2 and "模型 500" in t2 and "不算骚扰" in t2 and "【系统故障】这几天没有记录到" in t3, "给了故障记录 → 写这一节；给了空的 → 没有记录到")
big = [{"t": t0 + 7 * 3600 + k, "conv": P, "where": "私聊", "text": "很长的一句话" * 20} for k in range(1, 400)]
t4, _ = bj.block_input(dict(case, bot=big, limit=40))
hc = sum(len(x) for x in t4.splitlines() if x.startswith("    机器人 "))
check(0 < hc <= bj.BLOCK_BOT_CHARS + 200, f"机器人的话有上限（这次 {hc} 字）")

# ═══════════════ ③ 记机器人回他的话 ═══════════════
print("\n[③ 记机器人回他的话：他说过话才记、过期、删数据、拼卷宗带上]")
dd = Path(tempfile.mkdtemp())
ml = bl.MsgLog(dd / "m.json")
ml.note_bot(B, P, "没说过话的人 不记")
check(not ml.users.get(B), "他没说过话 → 不记")
say(ml, A, "在吗", conv=P, ago=300)
ml.note_bot(A, P, "在的", now=time.time() - 290)
ml.note_bot(A, P, "很早的一句", now=time.time() - 9 * 86400)
bf_ = ml.bot_for(A)
check([b["text"] for b in bf_] == ["在的"], "bot_for：窗口内的（9 天前的不要）", bf_)
ml.save(force=True)
ml2 = bl.MsgLog(dd / "m.json")
check([b["text"] for b in ml2.bot_for(A)] == ["在的"], "存盘 / 读回来")
n = ml2.forget(A)
check(n >= 2 and not ml2.bot_for(A), "删数据：机器人回他的话一起删", n)


async def main():
    # ── 拼卷宗带上机器人的话 ──
    st, ml, io, fl, _ = mk()
    say(ml, A, "尾巴借我摸一下嘛", conv=P, ago=500)
    ml.note_bot(A, P, "摸吧摸吧 轻点", now=time.time() - 490)
    case = fl.dossier("c1", A, "阿木", P, "测试", {"close": False})
    check([b.get("text") for b in case.get("bot") or []] == ["摸吧摸吧 轻点"] and case["bot"][0].get("where") == "私聊",
          "flow.dossier 带上机器人回他的话（写清楚在哪）", case.get("bot"))
    r = fl.file(A, "阿木", "老想摸我尾巴", P)
    await settle(io)
    check(io.calls and "    机器人 " in io.calls[-1][1] and "摸吧摸吧 轻点" in io.calls[-1][1], "法官收到的卷宗里有机器人跟着闹的那句", io.calls[-1][1][-600:] if io.calls else "")
    check(st.case(r["case"])["result"] == "放过" and not told(io, A), "判放过 → 不告诉他（他不知道有过案子）", io.told)

    # ── ④ 没判成 → 从轻放过 ──
    print("\n[④ 法官没判成 → 从轻放过（fail_release）；关掉 → 只冷却]")
    st, ml, io, fl, _ = mk()
    io.mode = "raise"
    say(ml, A, "骂人 BAD", ago=300)
    r = fl.file(A, "阿木", "骂我", G)
    await settle(io)
    c = st.case(r["case"])
    check(c["result"] == "放过" and not st.gate(A) and "从轻放过" in str(c.get("result_why")) and not told(io, A)
          and any("法官没判成" in x and "从轻放过" in x for x in io.notes),
          "调不通 → 从轻放过：不冷却、不告诉他、管理员那条写明（回退检查：v0.1.0 没判成也冷却）", c)
    check(any("法官没判成" in x and "从轻放过" in x for x in st.take_notes(A)), "给模型的那一行：法官没判成、从轻放过")
    st, ml, io, fl, _ = mk(cfg={"fail_release": False})
    io.mode = "raise"
    say(ml, A, "骂人 BAD", ago=300)
    r = fl.file(A, "阿木", "骂我", G)
    await settle(io)
    m = told(io, A)
    check(st.case(r["case"])["result"] == "没判成" and st.gate(A) == "cool" and len(m) == 1 and m[0].startswith("（系统消息）系统暂停了你和机器人")
          and "法庭" not in m[0], "fail_release 关掉 → 没判成：冷却、告诉他「系统暂停」（不说法庭）", m)

    # ── ⑤ 告诉被告 ──
    print("\n[⑤ 告诉被告：法庭口吻、原因只一个词；只说一次；真删之前先说；再审判删除也说；补告诉；没案子的冷却]")
    st, ml, io, fl, _ = mk(cfg={"notice_name": "小鱼"})
    say(ml, A, "你个废物 BAD", ago=300)
    r = fl.file(A, "阿木", "骂我", G)
    await settle(io)
    m = told(io, A)
    check(len(m) == 1 and m[0].startswith("（系统消息）法庭介入，暂停了你和小鱼在私聊和群里的对话（原因：辱骂），到 ")
          and "自动恢复" in m[0] and "法庭是独立判的，小鱼本人不参与" in m[0] and "/申诉" in m[0] and "管理员" in m[0]
          and "冲机器人骂人" not in m[0] and "庭审" not in m[0],
          "一审判冷却 → 私聊他：法庭介入、原因一个词、法庭独立判、怎么申诉；不发庭审意见（回退检查：v0.1.0 什么都不说）", m)
    check(m and m[0].count("\n") == 1 and len(m[0]) < 160, "简短：两行", m)
    await fl.block_notice(A)
    check(len(told(io, A)) == 1, "告诉过了 → 他私聊机器人时不再说")
    # 真删好友：先告诉他 → 再删
    st, ml, io, fl, _ = mk(cfg={"friend_mode": "auto", "delete_notice_wait": 0})
    cooled_before(st, ml, E)
    say(ml, E, "又骂 BAD", ago=100)
    say(ml, E, "还骂 BAD", ago=90)
    r = fl.file(E, "小堇", "又骂", G)
    await settle(io)
    c = st.case(r["case"])
    m = told(io, E)
    check(c["result"] == "删除" and io.order.index("告诉他") < io.order.index("删好友") and len(m) == 1
          and "判了永久不理并删除好友（原因：辱骂）" in m[0] and "验证消息" in m[0] and "/申诉" not in m[0],
          "二审判删除、真删好友 → 先告诉他（结果 + 删了以后在加好友的验证消息里申诉）再删；判完不重复发", (io.order, m))
    # 只提醒（不真删）→ 判完告诉他（私聊申诉那套）
    st, ml, io, fl, _ = mk()
    cooled_before(st, ml, E)
    say(ml, E, "又骂 BAD", ago=100)
    say(ml, E, "还骂 BAD", ago=90)
    r = fl.file(E, "小堇", "又骂", G)
    await settle(io)
    m = told(io, E)
    check(st.case(r["case"])["result"] == "删除" and "删好友" not in io.order and len(m) == 1 and "判了永久不理（原因：辱骂）" in m[0] and "/申诉" in m[0],
          "删好友只提醒 → 判完告诉他：永久不理、私聊 /申诉", m)
    # 再审判删除
    st, ml, io, fl, _ = mk()
    say(ml, A, "骂 BAD", ago=90000)  # v0.3.1：昨天骂的；下面通知之后又骂两句（判删除要前后 3 句、分布在 2 天）
    r = fl.file(A, "阿木", "骂我", G)
    await settle(io)
    n0 = len(told(io, A))
    fl.evidence(A, {"t": time.time(), "mid": "x1", "conv": G, "tag": bj.TAG_OWN, "text": "冷却了还骂 BAD"})
    fl.evidence(A, {"t": time.time(), "mid": "x2", "conv": G, "tag": bj.TAG_OWN, "text": "接着骂 BAD"})
    await settle(io, 8)
    c = st.case(r["case"])
    m = told(io, A)
    check(n0 == 1 and st.is_blocked_forever(A) and len(m) == 2 and "判了永久不理" in m[-1], "再审判删除 → 也告诉他（一审那条之外再说一次）", (c.get("retrials"), m))
    # 补告诉：判完那会儿没发出去（这里用「判的时候还没有 tell」）
    st, ml, io, fl, _ = mk()
    tell0 = io.tell
    io.tell = None
    say(ml, A, "骂 BAD", ago=300)
    fl.file(A, "阿木", "骂我", G)
    await settle(io)
    io.tell = tell0
    check(not told(io, A), "（判的时候没能说）")
    ok = await fl.block_notice(A)
    ok2 = await fl.block_notice(A)
    check(ok and not ok2 and len(told(io, A)) == 1 and "法庭介入" in told(io, A)[0], "他私聊机器人 → 补告诉一次（回退检查）", io.told)
    # 没案子的冷却（今天开庭到上限）
    st, ml, io, fl, _ = mk(scfg={"court_daily": 0})
    r = fl.file(B, "阿乙", "x", G)
    await fl.block_notice(B)
    await fl.block_notice(B)
    m = told(io, B)
    check(r["result"] == "只冷却" and len(m) == 1 and m[0].startswith("（系统消息）你和机器人的对话暂停中") and "法庭" not in m[0],
          "没案子的冷却 → 他来找机器人时说一句「暂停中」（只说一次、不说法庭）", m)
    # 不告诉：管理员 / 认 AI / 关掉
    st, ml, io, fl, _ = mk(cfg={"tell_user": False})
    say(ml, A, "骂 BAD", ago=300)
    fl.file(A, "阿木", "骂我", G)
    await settle(io)
    check(not io.told, "tell_user 关掉 → 不告诉他")

    # ── ⑥ 衔接 ──
    print("\n[⑥ 衔接：冷却到点 / 撤销 → 告诉他；没告诉过的不说；夜里到点的先记着]")
    st, ml, io, fl, _ = mk(scfg={"cool_hours": 0.0005})
    say(ml, A, "骂 BAD", ago=300)
    r = fl.file(A, "阿木", "骂我", G)
    await settle(io)
    st.found(A, True)
    st.found(A, False)
    await asyncio.sleep(2.2)
    n = await fl.cool_end_scan()
    n2 = await fl.cool_end_scan()
    m = told(io, A)
    check(n == 1 and n2 == 0 and len(m) == 2 and m[-1].startswith("（系统消息）法庭的暂停到点了，机器人现在恢复正常回你的消息。")
          and "暂停期间你发的消息机器人没看到" in m[-1] and "提醒：之后要是再越线" in m[-1],
          "冷却到点 → 告诉他恢复了（找过它 → 加一句；冷却过、会进二审 → 加提醒）；只说一次（回退检查：v0.1.0 什么都不说）", m)
    # 没告诉过他在冷却的 → 不说
    st, ml, io, fl, _ = mk(scfg={"cool_hours": 0.0005, "court_daily": 0})
    fl.file(B, "阿乙", "x", G)
    await asyncio.sleep(2.2)
    check(await fl.cool_end_scan() == 0 and not told(io, B), "他不知道自己在冷却（没告诉过）→ 到点也不说")
    # 夜里到点：先记着，他先来找就先说
    st, ml, io, fl, _ = mk(scfg={"cool_hours": 0.0005}, cfg={"bridge_night": "0-24"})
    say(ml, A, "骂 BAD", ago=300)
    fl.file(A, "阿木", "骂我", G)
    await settle(io)
    await asyncio.sleep(2.2)
    n = await fl.cool_end_scan()
    check(n == 0 and A in fl.due and len(told(io, A)) == 1, "夜里到点 → 先不说、记着")
    ok = await fl.cool_end_now(A)
    check(ok and len(told(io, A)) == 2 and "暂停到点了" in told(io, A)[-1] and A not in fl.due, "他先来找机器人 → 先发到点那条")
    # 撤销
    st, ml, io, fl, _ = mk()
    say(ml, A, "骂 BAD", ago=300)
    r = fl.file(A, "阿木", "骂我", G)
    await settle(io)
    await fl.appeal(A, "阿木", "我错了", "私聊「/申诉」")
    st.found(A, True)
    fl.unblock(A)
    await settle(io)
    m = told(io, A)
    check(len(m) == 2 and m[-1].startswith("（系统消息）管理员看过你的申诉，撤销了法庭的处理：暂停提前结束，机器人现在恢复正常回你的消息。")
          and "暂停期间你发的消息机器人没看到" in m[-1], "管理员撤销（他申诉过）→ 告诉他：看过你的申诉、暂停提前结束（回退检查）", m)
    st, ml, io, fl, _ = mk()
    say(ml, B, "骂 BAD", ago=300)
    fl.file(B, "阿乙", "骂我", G)
    await settle(io)
    fl.unblock(B)
    await settle(io)
    m = told(io, B)
    check(len(m) == 2 and "管理员复核后撤销了法庭的处理：暂停提前结束" in m[-1] and "\n" not in m[-1], "没申诉、管理员自己撤的 → 复核后撤销；没找过它 → 一行", m)
    st, ml, io, fl, _ = mk()
    st.start_cool(C, "阿丙", "x", G, hours=1)
    fl.unblock(C)
    await settle(io)
    check(not told(io, C), "没告诉过他在冷却 → 撤销了也不说")

    # ── ⑦ 找过几次 / 申诉回执 / 配置 ──
    print("\n[⑦ 找过几次、申诉回执、管理员立案默认关、配置]")
    st, ml, io, fl, _ = mk()
    check(not st.found(D, True), "没在冷却的人 → 不记")
    st.start_cool(D, "阿丁", "x", G, hours=1)
    st.found(D, True)
    st.found(D, True)
    st.found(D, False)
    check((st.cool_list().get(D) or {}).get("found") == {"p": 2, "g": 1}, "冷却中找过几次：私聊 2、群里 1（只记次数）", st.cool_list().get(D))
    at = bf.appeal_tip_text(7)
    check(at.startswith("（系统消息）你的申诉已经转给管理员了") and "法庭的决定照旧" in at and "7 天内只转一次" in at, "申诉回执：法庭的决定照旧", at)
    check(bf.FLOW_DEFAULTS["admin_file"] is False and bf.FLOW_DEFAULTS["appeal_tip"] is True and bf.FLOW_DEFAULTS["fail_release"] is True
          and bf.FLOW_DEFAULTS["tell_user"] is True and bf.FLOW_DEFAULTS["bridge"] is True, "默认：管理员立案关、申诉回执开、没判成放过、告诉他开、衔接开")
    fl2 = bf.BlockCourt(st, ml, io, cfg={"bridge_night": "23-7", "notice_name": "  小鱼  "})
    check(fl2.cfg["bridge_night"] == "23-7" and fl2._nm() == "小鱼", "字的配置照写（不当成 off / shadow / auto）", fl2.cfg)
    sc = json.loads((d / "_conf_schema.json").read_text(encoding="utf-8"))
    bk, ap = sc["block"]["items"], sc["appeal"]["items"]
    check(bk["admin_file"]["default"] is False and bk["fail_release"]["default"] is True and bk["tell_user"]["default"] is True
          and bk["bridge"]["default"] is True and bk["bridge_night"]["default"] == "0-8" and ap["tip"]["default"] is True,
          "配置里有这几项、默认值对得上")
    main = (d / "main.py").read_text(encoding="utf-8")
    check('self.flow.on("admin_file")' in main and "async def tell(self, uid, text)" in main and "self.msglog.note_bot(" in main
          and "露骨的色情" in main and "cool_end_scan" in main and "block_notice" in main,
          "外壳接上了：管理员立案开关、私聊他、记机器人回他的话、立案工具只写红线、每分钟看冷却到点、门口补告诉")
    v1 = bf.verdict_text("冷却", time.time() + 3600, "一审", "其他")
    v2 = bf.verdict_text("冷却", time.time() + 3600, "二审", "擦边")
    check("原因" not in v1 and "（原因：越线的内容）" in v2, "原因：其他 / 无不写；老案子的擦边 → 越线的内容")

    # ── ⑧ 他先来就先说 / 开机补告诉 / 申诉「之前的已经转了」──
    print("\n[⑧ 冷却到点他一来就先发（不等定时）、开机补告诉、又申诉回一句「之前的已经转了」]")
    st, ml, io, fl, _ = mk(scfg={"cool_hours": 0.0005})
    say(ml, A, "骂 BAD", ago=300)
    fl.file(A, "阿木", "骂我", G)
    await settle(io)
    await asyncio.sleep(2.2)
    ok = await fl.cool_end_now(A)
    ok2 = await fl.cool_end_now(A)
    n = await fl.cool_end_scan()
    m = told(io, A)
    check(ok and not ok2 and n == 0 and len(m) == 2 and "暂停到点了" in m[-1],
          "白天：冷却刚到点、定时还没扫到，他先来找机器人 → 先发到点那条；只发一次，定时扫到也不再发（回退检查：以前要等定时）", m)
    check(not await fl.cool_end_now(B) and not told(io, B), "没在冷却的人来了 → 什么都不发")
    st, ml, io, fl, _ = mk(scfg={"cool_hours": 0.0005, "court_daily": 0})
    fl.file(B, "阿乙", "x", G)
    await asyncio.sleep(2.2)
    check(not await fl.cool_end_now(B) and not told(io, B), "他不知道自己在冷却（没告诉过）→ 他来了也不说到点")
    # 开机补告诉：判的时候没发出去（比如不是好友）→ 开机以后补一次
    st, ml, io, fl, _ = mk()
    keep = io.tell

    async def nope(uid, text):
        return False

    io.tell = nope
    say(ml, A, "骂 BAD", ago=300)
    fl.file(A, "阿木", "骂我", G)
    await settle(io)
    io.tell = keep
    n0 = len(told(io, A))
    n = await fl.notice_backfill()
    n2 = await fl.notice_backfill()
    m = told(io, A)
    check(n0 == 0 and n == 1 and n2 == 0 and len(m) == 1 and m[0].startswith("（系统消息）法庭介入，暂停了你和机器人"),
          "开机补告诉：冷却中、结果还没告诉过他的 → 补一次（回退检查：v0.1.0 没有）", m)
    st, ml, io, fl, _ = mk(scfg={"cool_hours": 1})
    t0 = time.time() - 60
    cid = st.open_case(C, "阿丙", "x", G, now=t0, cool_until=t0 + 600)
    st.start_cool(C, "阿丙", "x", G, now=t0, hours=0.2, case=cid)
    fl.start_ts = time.time()
    await fl.boot()
    n = await fl.notice_backfill()
    m = told(io, C)
    check((st.case(cid) or {}).get("result") == "没判成" and n == 1 and len(m) == 1
          and m[0].startswith("（系统消息）系统暂停了你和机器人") and "法庭" not in m[0],
          "庭审到一半重启了（没判成、冷却）→ 开机补告诉他「系统暂停」（不说法庭）", m)
    # 又申诉
    st, ml, io, fl, _ = mk()
    say(ml, A, "骂 BAD", ago=300)
    fl.file(A, "阿木", "骂我", G)
    await settle(io)
    r1 = await fl.appeal(A, "阿木", "我错了", "私聊「/申诉」")
    r2 = await fl.appeal(A, "阿木", "再说一次", "私聊「/申诉」")
    r3 = await fl.appeal(E, "阿戊", "我没被关", "私聊「/申诉」")
    ag = bf.appeal_again_text()
    check(r1.get("ok") and not r2.get("ok") and r2.get("again") and not r3.get("ok") and not r3.get("again")
          and ag.startswith("（系统消息）你之前的申诉已经转给管理员了") and "不再重复转" in ag,
          "又申诉：不转、标出「之前的已经转了」（外壳回他一条，12 小时最多一次）；没被冷却的人不算", (r1, r2, r3))
    check(st.told_at("x:none") == 0 and st.mark_told("x:1", 1000.0) and st.told_at("x:1") == 1000.0, "told_at：上次告诉他是几点")
    main = (d / "main.py").read_text(encoding="utf-8")
    check("await self.flow.cool_end_now(uid)" in main and "notice_backfill()" in main and "appeal_again_text()" in main
          and "await self._tell_user(uid, tip)" in main,
          "外壳：每条消息先问冷却到点那条、开机补告诉、又申诉回一句、群里申诉的回执私聊发")


asyncio.run(main())
bad = [m for ok, m in RES if not ok]
print("\n" + "=" * 60)
print(f"  结果：{len(RES) - len(bad)} 项过了，" + ("全过" if not bad else f"{len(bad)} 项没过："))
for m in bad:
    print("    - " + m)
print("=" * 60)
sys.exit(1 if bad else 0)
