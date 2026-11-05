# -*- coding: utf-8 -*-
"""
拉黑庭的流程（不依赖 AstrBot，可以单独测；跟外面打交道的都走 io，测试里换成假的）

照作者自己的机器人 v1.17.13 的流程（11-03 作者批的）做的通用版：
  立案 file：模型调工具 / 管理员用指令 → 先不回他（占位：冷却 hold 秒，hold 至少比法官最多等的久）→ 后台开庭
  一审：放过（马上恢复、什么都不留）/ 冷却（从判完起 cool_hours 小时）
  冷却中：他冲机器人说的话只当证据收（evidence；不回、不进对话记录、不给主模型看）；说完安静 retrial_quiet_sec 秒 → 再审
  再审：删除 / 不动（道歉、问为什么不理他、抱怨被冷落都不算越线；一个案子最多 retrial_max 次）
  冷却到点、没被删 → 冷却过；冷却过的人又被立案 → 直接二审：删除 / 保留（保留 + 核实过 ≥1 句 → 冷却；一句都没有 → 放过）
  法官没判成（没配模型 / 调不通 / 超时 / 格式不对）：一审、二审 → 只冷却、绝不升级；再审 → 不动
  删除 = 永久不理（真做）+ 删好友（off / shadow 只提醒 / auto 真删）+ 清数据（off / shadow 只提醒 / auto 真清）
        先永久不理 → 再删好友 → 最后清数据；每个 await 之后重查撤销、删数据
  每天上限：开庭 court_daily 个（超了只冷却、不开庭）、删除 delete_daily 个（超了只冷却 / 再审不动）
  永远不删：AstrBot 管理员（不立案）、白名单（也走一审，但冷却中不收证据、不再审、不进二审）
  认 AI 插件认出来的机器人：不立案、不收证据
  通知：每个案子私聊告诉管理员（判词、核实几句、结果）；判完以后他下次找机器人时给模型一行（只写事实）
  重启：审到一半 → 没判成（冷却）、庭审期间被撤销的不冷却；冷却中的证据不丢、接着按规矩再审
io 要有：judge(system, text, timeout, meta) / notify(text) / is_friend(uid) / delete_friend(uid) / wipe(uid) /
        is_ai(uid) / is_admin(uid) / protected(uid) / spawn(coro) / log(text)
"""

import asyncio
import re
import time

from .block_judge import STAGE1, STAGE2, STAGE_RE, judge_block
from .block_log import where_map
from .block_store import EV_MAX, day_of, md

FLOW_DEFAULTS = {
    "enabled": True,
    "retrial": True,  # 冷却中收证据、再审
    "second": True,  # 冷却过的人再被立案 → 二审
    "hold_sec": 180,  # 占位至少几秒（至少按 judge_timeout_sec + 60 算）
    "judge_timeout_sec": 90.0,  # 法官最多等几秒
    "case_msgs": 40,  # 卷宗里最多放他几条话
    "friend_mode": "shadow",  # 删除时删好友：off 不删 / shadow 只私聊提醒管理员「本来会删」/ auto 真删
    "wipe_mode": "shadow",  # 删除时清数据：off 不清 / shadow 只提醒「本来会清」/ auto 真清
    "notify": True,  # 每个案子私聊告诉管理员
    "tell_model": True,  # 判完以后，他下次找机器人时给模型一行（只写事实）
    "appeal": True,  # 申诉
    "appeal_tip": False,  # 申诉的人收到一条系统提示「已转给管理员」（不是机器人说话）
}
MODES = ("off", "shadow", "auto")
WIPED = "（删数据时抹掉了）"


def _clip(t, n):
    return re.sub(r"\s+", " ", str(t or "")).strip()[:n]


def mode(v, default="shadow"):
    """off / shadow / auto（中文的「关 / 只提醒 / 真做」也认）；写错了当 shadow"""
    s = str(v or "").strip().lower()
    s = {
        "关": "off",
        "不删": "off",
        "只提醒": "shadow",
        "影子": "shadow",
        "真删": "auto",
        "真清": "auto",
        "真做": "auto",
    }.get(s, s)
    return s if s in MODES else default


def reason_for_model(r):
    """法官的理由给模型看之前：去掉引号里的话；带「可以回 / 建议 / 你可以…」这种建议的 → 整句不给（理由照样给管理员）"""
    r = re.sub(r"「[^」]*」|『[^』]*』|“[^”]*”|\"[^\"]*\"", "", str(r or "")).strip(
        " ，,。；;：:"
    )
    if re.search(
        r"(可以|不妨|建议|最好|应该|就)(回|说|告诉|跟他|对他|让他|骂|怼|无视|不理)|回他|跟他说|对他说|你可以|它可以|你该|它该",
        r,
    ):
        return ""
    return r[:60]


class BlockCourt:
    def __init__(self, store, msglog, io, cfg=None, clock=time.time):
        self.store = store
        self.msglog = msglog
        self.io = io
        self.cfg = dict(FLOW_DEFAULTS)
        self.set_cfg(cfg or {})
        self.clock = clock
        self.lock = asyncio.Lock()
        self.tasks = {}  # 案号 → 再审的去抖任务
        self.start_ts = clock()
        self._cap_told = {}

    # ───────── 配置 ─────────
    def set_cfg(self, cfg):
        for k, v in (cfg or {}).items():
            if k in FLOW_DEFAULTS and v is not None:
                d = FLOW_DEFAULTS[k]
                try:
                    if isinstance(d, bool):
                        self.cfg[k] = (
                            v
                            if isinstance(v, bool)
                            else str(v).strip().lower()
                            in ("1", "true", "yes", "on", "开")
                        )
                    elif isinstance(d, str):
                        self.cfg[k] = mode(v, d)
                    else:
                        self.cfg[k] = type(d)(v)
                except (TypeError, ValueError):
                    pass

    def on(self, k):
        return bool(self.cfg.get(k, FLOW_DEFAULTS.get(k)))

    def _f(self, k):
        if k in FLOW_DEFAULTS:
            try:
                return float(self.cfg.get(k, FLOW_DEFAULTS[k]))
            except (TypeError, ValueError):
                return float(FLOW_DEFAULTS[k])
        return self.store.f(k)

    def hold_sec(self):
        return max(self._f("hold_sec"), self._f("judge_timeout_sec") + 60)

    def log(self, text):
        try:
            self.io.log(text)
        except Exception:
            pass

    def _spawn(self, coro):
        try:
            return self.io.spawn(coro)
        except Exception:
            coro.close()
            return None

    def _who(self, name, uid):
        return f"「{name}」({uid})" if name else f"({uid}，他的数据删了)"

    # ───────── 立案 ─────────
    def file(self, uid, name="", why="", conv="", by="机器人", now=None):
        """立案（模型调工具 / 管理员指令）→ {"result", "why", "case", "until", "stage", "protect"}
        result：立案 / 只冷却（今天开庭到上限 / 案子没记上）/ 已经在名单里 / 已经在冷却 / 已经在审理 / 管理员 / 机器人 / 关着 / 存档坏了 / 不知道是谁
        从查重到立案之间没有 await（同一个人同时被立两次案：第二次看得到第一次的占位，不另开庭）"""
        if not self.on("enabled"):
            return {"result": "关着", "why": "拉黑庭关着"}
        uid = re.sub(r"\D", "", str(uid or ""))
        if not uid:
            return {"result": "不知道是谁", "why": "没有 QQ 号"}
        why = _clip(why, 40) or "没写原因"
        now = float(now if now is not None else self.clock())
        st = self.store
        if st.broken:
            return {"result": "存档坏了", "why": f"拉黑庭的存档{st.broken}：这次不立案"}
        if st.is_blocked_forever(uid):
            return {
                "result": "已经在名单里",
                "why": "他已经是永久不理的人了（不另开庭）",
            }
        if st.open_case_of(uid, now):
            return {"result": "已经在审理", "why": "他的案子还在审（不另开庭）"}
        if st.cooling(uid, now):
            return {
                "result": "已经在冷却",
                "why": f"他正在冷却（到 {md(st.cooling(uid, now))}，不另开庭）",
            }
        if self.io.is_admin(uid):
            st.note_try(uid, name, why, "是管理员：不立案、不冷却", now, by)
            self.log(
                f"{by}想把「{name}」({uid}) 交给拉黑庭（{why}）→ 是管理员：不立案、不冷却"
            )
            return {"result": "管理员", "why": "他是管理员：不立案、不冷却"}
        if self.io.is_ai(uid):
            st.note_try(uid, name, why, "认 AI 认出是机器人：不立案", now, by)
            self.log(
                f"{by}想把「{name}」({uid}) 交给拉黑庭（{why}）→ 认 AI 插件认出他是别人家的机器人：不立案"
            )
            return {
                "result": "机器人",
                "why": "认 AI 插件认出他是别人家的机器人：不立案",
            }
        protect = self.io.protected(uid)
        day, cap = day_of(now), int(st.f("court_daily"))
        n_today = st.cases_on(day)
        if (
            n_today >= cap
        ):  # 今天开庭的到上限了：只冷却、不开庭（第一次超的时候告诉管理员一次）
            until = st.start_cool(uid, name, why, conv, now=now, by=by)
            if not until:
                return {"result": "存档坏了", "why": "存档写不进去：这次没冷却"}
            st.note_try(
                uid,
                name,
                why,
                f"今天开庭到上限（{cap} 个）：只冷却到 {md(until)}",
                now,
                by,
            )
            self.log(
                f"「{name}」({uid})（{why}）→ 今天已经开庭 {n_today} 次（每天最多 {cap} 次）：只冷却到 {md(until)}、不开庭"
            )
            self._note(
                uid,
                f"{self._head(by, now, why)}今天开庭的次数到上限了：系统只让你们冷却到 {md(until)}，不升级",
            )
            self._cap_told[day] = self._cap_told.get(day, 0) + 1
            if self._cap_told[day] == 1 and self.on("notify"):
                self._spawn(
                    self.io.notify(
                        f"【法庭·拉黑庭】今天开庭的次数到上限了（每天最多 {cap} 次，配置「每天最多开庭几次」）："
                        f"今天之后再立案只冷却、不开庭（明天恢复）。这次是{self._who(name, uid)}：{why} → 冷却到 {md(until)}"
                    )
                )
            return {"result": "只冷却", "until": until, "why": "今天开庭到上限"}
        stage = (
            STAGE2
            if (self.on("second") and not protect and st.cooled_of(uid, now))
            else STAGE1
        )
        hold = self.hold_sec()
        cid = st.new_cid(now)
        until = st.start_cool(
            uid, name, why, conv, now=now, hours=hold / 3600, case=cid, by=by
        )
        if not until:
            return {"result": "存档坏了", "why": "存档写不进去：这次没立案"}
        evok = self.on("retrial") and not protect
        if not st.open_case(
            uid,
            name,
            why,
            conv,
            now=now,
            cool_until=until,
            stage=stage,
            evok=evok,
            cid=cid,
            by=by,
        ):
            until = st.start_cool(uid, name, why, conv, now=now, by=by) or until
            self.log(
                f"「{name}」({uid})（{why}）→ 案子记不上 → 只冷却到 {md(until)}、不开庭"
            )
            return {"result": "只冷却", "until": until, "why": "案子没记上"}
        st.note_try(
            uid, name, why, f"立案 {cid}（{stage}）：先不回他、等法官判", now, by
        )
        self.log(
            f"{by}把「{name}」({uid}) 交给拉黑庭（{why}）→ 立案 {cid}（{stage}"
            + (f"；{protect}：只走一审、不会被删除" if protect else "")
            + f"）：先不回他（占位到 {md(until, '%H:%M:%S')}）、后台开庭"
        )
        info = {"close": bool(protect), "first": self.msglog.first_seen(uid), "by": by}
        if stage == STAGE2:
            info["prev"] = st.cooled_of(uid, now)
        self._spawn(self._trial(cid, conv, uid, name, why, now, until, info))
        return {
            "result": "立案",
            "case": cid,
            "until": until,
            "stage": stage,
            "protect": protect,
        }

    def _head(self, by, t0, why):
        return f"{'你' if by == '机器人' else '管理员'} {md(t0)} 提出不理他（{why}）—— "

    def _note(self, uid, text):
        if self.on("tell_model") and text:
            self.store.add_note(uid, text)

    # ───────── 卷宗 ─────────
    def dossier(self, cid, uid, name, conv, why, info, now=None):
        """一审 / 二审的卷宗材料 → block_judge.block_input 要的 case
        他的话：记下来的 window_days 天里的（撤销之前的不要；二审只要上次冷却到期以后的）；群号不写进卷宗（群 A / 群 B）"""
        now = float(now if now is not None else self.clock())
        st = self.store
        days = int(st.f("window_days"))
        pd = st.pardoned_at(uid)
        prev = info.get("prev") if isinstance(info.get("prev"), dict) else None
        since = float(prev.get("last") or 0) if prev else 0.0
        lim = max(now - days * 86400, pd + 0.001, since)
        where = where_map()
        here = where(conv)
        msgs = [
            dict(m, where=where(m.get("conv")))
            for m in self.msglog.msgs_for(uid, since=lim, now=now)
        ]
        ctx = self.msglog.ctx_for(conv)
        clues = [
            {"mid": x.get("mid"), "kinds": list(x.get("kinds") or [])}
            for x in st.marks(uid, now)
        ]
        prevs = [
            {
                "t": c.get("t"),
                "verdict": c.get("verdict") or "",
                "reason": c.get("reason") or "",
                "result": c.get("result") or c.get("state") or "",
            }
            for c in st.cases(uid, after=pd)
            if c.get("id") != cid
        ]
        pc = None
        if prev:
            pc = {"n": prev.get("n"), "last": prev.get("last")}
            c0 = st.case(str(prev.get("case") or "")) or {}
            if c0 and not c0.get("wiped"):
                pc.update(
                    t=c0.get("t"),
                    why=c0.get("why"),
                    stage=c0.get("stage") or STAGE1,
                    verdict=c0.get("verdict"),
                    reason=c0.get("reason"),
                    quotes=c0.get("quotes") or [],
                )
        return {
            "uid": uid,
            "name": name,
            "names": self.msglog.names_of(uid),
            "close": info.get("close"),
            "ai": False,
            "first": info.get("first"),
            "prev": prevs,
            "why": why,
            "by": info.get("by") or "机器人",
            "since": since or None,
            "prev_cool": pc,
            "clues": clues,
            "msgs": msgs,
            "ctx": ctx,
            "where": here,
            "now": now,
            "days": days,
            "limit": int(self._f("case_msgs")),
        }

    async def _judge(self, case, conv, uid, meta):
        to = max(5.0, self._f("judge_timeout_sec"))

        async def llm(system, text):
            return await self.io.judge(system, text, to, meta)

        return await asyncio.wait_for(
            judge_block(case, llm, self.store.f("cool_hours"), log=self.log), to + 20
        )

    # ───────── 一审 / 二审 ─────────
    async def _trial(self, cid, conv, uid, name, why, t0, until, info):
        """后台庭审：卷宗 → 法官 → 锁里：核对引用 → 封顶 → 执行 → 结案 → 锁外：告诉管理员、给模型留一行；判了冷却 → 接着按冷却中的规矩"""
        v, fail, meta = None, "", {}
        st = self.store
        stage = (st.case(cid) or {}).get("stage") or STAGE1
        try:
            case = self.dossier(cid, uid, name, conv, why, info)
            case["stage"] = stage
            v = await self._judge(case, conv, uid, meta)
            if not v.get("ok"):
                fail = v.get("why") or "法官没判成"
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            fail = "法官超时"
        except Exception as e:
            fail = f"法官出错（{type(e).__name__}）"
        try:  # 判完了、等锁之前把占位续上（别的案子在锁里删好友可能要一阵；只续这个案子的那一项）
            st.keep_hold(uid, cid, self.clock() + 600)
        except Exception as e:
            self.log(f"续占位出错：{e!r}"[:120])
        out, stt = None, {"done": ""}
        async with self.lock:
            try:
                out = await self._exec(
                    cid,
                    conv,
                    uid,
                    name,
                    why,
                    t0,
                    until,
                    v,
                    fail,
                    meta,
                    stt,
                    info.get("by") or "机器人",
                )
            except Exception as e:
                self.log(f"执行出错：{e!r}"[:150])
                did = stt.get("done") or (
                    "删除"
                    if (st.blocked_list().get(uid) or {}).get("case") == cid
                    else ""
                )
                res = did or "没判成"
                rwhy = f"执行到一半出错（{type(e).__name__}）" + (
                    "：已经执行了、记录可能没写全" if did else ""
                )
                u2 = until
                if not did:
                    u2 = st.start_cool(uid, name, why, conv, case=cid) or until
                st.close_case(
                    cid, result=res, result_why=rwhy, cool_until=int(u2), ev=[]
                )
                out = {
                    "result": res,
                    "why": rwhy,
                    "v": v if did else None,
                    "ok": [],
                    "cp": {},
                    "meta": meta,
                    "broken": True,
                    "until": u2,
                    "stage": stage,
                }
        try:
            await self._tell(
                cid, conv, uid, name, why, t0, until, out, info.get("by") or "机器人"
            )
        except Exception as e:
            self.log(f"告诉管理员出错：{e!r}"[:120])
        if (
            out
            and out.get("result") == "冷却"
            and (st.case(cid) or {}).get("state") == "冷却中"
        ):
            self._rt_kick(
                cid
            )  # 占位期间他冲机器人说过话：判了冷却以后照冷却中的规矩等他说完再审
        return out

    def _keep_safe(self, uid):
        """删除之前再看一眼保护：管理员、白名单（庭审期间加进去的也算）→ 不能删的原因 / \"\""""
        if self.io.is_admin(uid):
            return "他是管理员：不会被删除"
        p = self.io.protected(uid)
        return f"{p}：不会被删除" if p else ""

    async def _exec(self, cid, conv, uid, name, why, t0, until, v, fail, meta, stt, by):
        """锁里：重查（撤销了 / 已经在名单里 / 删了数据）→ 核对引用 → 封顶 → 执行 → 结案"""
        st = self.store
        now = self.clock()
        c0 = st.case(cid) or {}
        stage = c0.get("stage") or STAGE1
        table = (v or {}).get("table") or {}
        ok, cp, extra, judged = [], {"result": "", "why": "", "n": 0, "days": 0}, {}, 0
        till = until
        wiped = bool(c0.get("wiped"))
        if wiped:
            name, why = "", WIPED
        if st.pardoned_at(uid) >= t0 or c0.get("pardoned"):
            res, rwhy = "撤销了", "管理员撤销了（庭审期间）"
        elif st.is_blocked_forever(uid):
            res, rwhy = "已经在名单里", ""
        elif fail:
            res, rwhy = "没判成", fail
        else:
            ok = st.verify(v.get("cites"), table, uid, now)
            cp = st.trial_caps(stage, v.get("verdict"), ok)
            ok = cp.get("ok", ok)
            res, rwhy = cp["result"], cp["why"]
        friend = None
        if (
            res == "删除"
        ):  # 先查好友（要 await）—— 保护、每天上限都在查完以后看（庭审期间 / 这几秒里进了白名单也算）
            friend = await self._friend_of(uid)
            if not wiped and (st.case(cid) or {}).get(
                "wiped"
            ):  # 查好友那几秒里删了他的数据：下面不再写名字、原因、原话
                wiped, name, why = True, "", WIPED
            pw = self._keep_safe(uid)
            if pw:
                res, rwhy = ("冷却" if len(ok) >= int(st.f("cool_min")) else "放过"), pw
        if res == "删除":
            dn, daily = st.deleted_today(now), max(0, int(st.f("delete_daily")))
            if dn >= daily:
                res, rwhy = (
                    "冷却",
                    f"今天已经删除了 {dn} 个（每天最多 {daily} 个）：只冷却",
                )
        if res == "删除":
            extra = await self._remove(
                cid, conv, uid, name, why, t0, ok, now, stt, by, friend
            )
            res, rwhy = extra.get("result") or res, extra.get("why") or rwhy
            till = extra.get("until") or till
            judged = extra.get("judged") or 0
            if extra.get("wiped"):
                wiped, name, why, ok = True, "", WIPED, []
        elif res in ("冷却", "没判成"):
            u2 = st.start_cool(
                uid, name, why, "" if wiped else conv, now=now, case=cid, by=by
            )
            if u2:
                till = u2
                stt["done"] = "冷却"
                if res == "冷却":
                    judged = st.judged_add(uid, "冷却", now)
                    st.mark_cooled(uid, u2, cid, now)
            else:
                rwhy = (
                    rwhy + "；" if rwhy else ""
                ) + "存档写不进去：冷却没记上（占位到点就恢复）"
        elif res == "放过":
            st.end_cool(uid, cid, now)
        vd = (v or {}).get("verdict", "") if (v and not fail) else ""
        reason = "" if wiped else ((v or {}).get("reason", "") if vd else "")
        fields = dict(
            verdict=vd,
            kind=(v or {}).get("kind", "") if vd else "",
            reason=reason,
            cites=list((v or {}).get("cites") or []) if vd else [],
            n_ok=cp.get("n", 0),
            days=cp.get("days", 0),
            result=res,
            result_why=rwhy,
            cool_until=int(till),
            model=meta.get("model", ""),
            ms=meta.get("ms") or (v or {}).get("ms") or 0,
        )
        if (
            ok and not wiped and res in ("冷却", "删除")
        ):  # 核实过的原话（再审 / 二审给法官看「立案时的情况」「上次冷却」用；每句 ≤60 字）
            fields["quotes"] = [
                {"n": m.get("n"), "t": m.get("t"), "text": _clip(m.get("text"), 60)}
                for m in ok[:6]
            ]
        for k in ("friend", "wipe"):
            if extra.get(k):
                fields[k] = extra[k]
        if res in ("放过", "没判成", "已经在名单里", "撤销了"):
            fields["ev"] = []  # 放过：什么都不留；别的不会再审，占位期间收的用不上
        if (
            res == "冷却"
            and c0.get("evok")
            and stt.get("done") == "冷却"
            and till > now
        ):
            st.update_case(
                cid, state="冷却中", **fields
            )  # 冷却期间收证据 / 再审；到点由去抖 / 开机 / 下次立案顺手结案
        else:
            st.close_case(cid, **fields)
        self.log(
            f"案子 {cid}（{stage}）{self._who(name, uid)}："
            + (
                f"法官判 {vd}（{(v or {}).get('kind')}：{reason}）引用 {(v or {}).get('cites')} 核实 {cp.get('n', 0)} 句 → "
                if vd
                else ""
            )
            + f"{res}"
            + (f"（{rwhy}）" if rwhy else "")
        )
        return {
            "result": res,
            "why": rwhy,
            "v": v if vd else None,
            "ok": [] if wiped else ok,
            "cp": cp,
            "meta": meta,
            "wiped": wiped,
            "until": till,
            "stage": stage,
            "judged": judged,
            "friend": extra.get("friend", ""),
            "wipe": extra.get("wipe", ""),
            "evok": bool(c0.get("evok")),
        }

    async def _friend_of(self, uid):
        """他是不是机器人的 QQ 好友（删好友关着 → 不查）→ True / False / None 查不到"""
        if self.cfg.get("friend_mode") == "off":
            return None
        try:
            return await self.io.is_friend(uid)
        except Exception:
            return None

    async def _remove(
        self, cid, conv, uid, name, why, t0, ok, now, stt=None, by="机器人", friend=None
    ):
        """删除（锁里调；好友已经查过了）：先永久不理 → 再删好友（friend_mode；是好友才删）→ 最后清数据（wipe_mode）
        查好友那一步、每个 await 之后都重查撤销和删数据（删了 → 名单 / 记录 / 通知里不写名字、原因、原话）
        → {"result": 删除 / 冷却（名单写不进去）/ 撤销了, "why", "friend", "wipe", "wiped", "judged", "until"}"""
        st = self.store
        stt = stt if isinstance(stt, dict) else {}
        out = {
            "result": "删除",
            "why": "",
            "friend": "",
            "wipe": "",
            "wiped": False,
            "judged": 0,
        }
        fm, wm = self.cfg.get("friend_mode"), self.cfg.get("wipe_mode")

        def gone():
            return bool((st.case(cid) or {}).get("wiped"))

        def pardoned():
            return st.pardoned_at(uid) >= t0

        if gone():
            name, why, ok, out["wiped"] = "", WIPED, [], True
        if pardoned():
            return dict(out, result="撤销了", why="管理员撤销了（庭审期间）")
        w = out["wiped"]
        if not st.add_block(
            uid,
            st.entry(
                uid, name, why, "" if w else conv, [] if w else ok, now, case=cid, by=by
            ),
        ):
            u2 = st.start_cool(
                uid, name, why, "" if w else conv, now=now, case=cid, by=by
            )
            return dict(
                out,
                result="冷却",
                why="存档写不进去 → 没进永久不理的名单（照旧冷却）",
                until=u2 or 0,
            )
        stt["done"] = "删除"
        out["judged"] = st.judged_add(uid, "删除", now)
        if fm == "off":
            out["friend"] = "关着（不删好友）"
        elif friend is True:
            if fm != "auto":
                out["friend"] = "本来会删（只提醒，没真删）"
            else:
                r, err = await self._delete_friend(uid, name, cid, now, t0)
                out["friend"] = r
                out["why"] = err if r != "删了" else ""
                if gone() and not w:
                    w = out["wiped"] = True
        elif friend is False:
            out["friend"] = "不是好友（不用删）"
        else:
            out["friend"] = "查不到好友列表（没删）"
        plan = {
            "off": "关着（不清）",
            "shadow": "本来会清（只提醒，没真清）",
            "auto": "清数据中",
        }.get(wm, "本来会清（只提醒，没真清）")
        st.note_delete(
            uid,
            "" if w else name,
            WIPED if w else why,
            now,
            case=cid,
            friend=out["friend"],
            wipe=plan,
            ms=[] if w else ok,
        )
        if pardoned():
            return dict(
                out,
                result="撤销了",
                why=f"执行到一半管理员撤销了（删好友：{out['friend']}）",
                wipe="撤销了（没清）",
            )
        if wm == "auto":
            try:
                rep = await self.io.wipe(uid)
                out["wipe"] = "清了" + (f"（{_clip(rep, 60)}）" if rep else "")
            except Exception as e:
                out["wipe"] = f"清数据出错（{type(e).__name__}）"
            out["wiped"] = True
            st.note_delete_update(cid, wipe=out["wipe"])
        else:
            out["wipe"] = plan
        return out

    async def _delete_friend(self, uid, name, cid, now, t0):
        """真删好友（auto）：先落盘「进行中」（同一个人同一天只调一次）→ 调之前再查一次撤销 → delete_friend
        → 出错 / 超时：再查一次好友列表 → (删了 / 没删 / 不确定 / 今天已经处理过他的删好友 / 撤销了, 出错的说明)"""
        st = self.store
        day = day_of(now)
        if not st.pending_start(uid, day, {"case": cid, "name": name}, now):
            return "今天已经处理过他的删好友", ""
        if t0 and st.pardoned_at(uid) >= t0:
            st.pending_end(uid, day, "撤销了", "删之前管理员撤销了")
            return "撤销了", "删之前管理员撤销了"
        okd, err = False, ""
        try:
            okd, err = await self.io.delete_friend(uid)
        except Exception as e:
            okd, err = False, repr(e)[:80]
        if okd:
            res = "删了"
        else:
            try:
                fs = await self.io.is_friend(uid)
            except Exception:
                fs = None
            res = "删了" if fs is False else ("没删" if fs is True else "不确定")
        st.pending_end(uid, day, res, err)
        self.log(
            f"删好友{self._who(name, uid)}：{res}"
            + (f"（{err}；查了好友列表）" if err else "")
        )
        return res, err

    # ───────── 通知 ─────────
    def _did(self, res, rwhy, out, till):
        if res == "冷却":
            return f"冷却到 {md(till)}（所有群和私聊不理他）" + (
                f"；冷却期间他冲机器人说的话只当证据收着（机器人看不到），说完安静 {int(self.store.f('retrial_quiet_sec')) // 60} 分钟再审"
                f"（最多 {int(self.store.f('retrial_max'))} 次）"
                if out.get("evok")
                else "；他在白名单里 / 再审关着：冷却中不收证据、不再审、不会被删除"
            )
        return {
            "放过": "放过 → 不冷却、马上恢复正常（什么都不留）",
            "删除": f"删除：永久不理他（所有群和私聊都不理、他再加好友自动拒绝）；删好友：{out.get('friend') or '—'}；清数据：{out.get('wipe') or '—'}",
            "没判成": f"法官没判成（{rwhy}）→ 不升级，只冷却到 {md(till)}",
            "撤销了": "你撤销了（庭审期间）→ 不执行",
            "已经在名单里": "他已经在永久不理的名单里了 → 这个案子不再执行",
        }.get(res, res)

    def _judge_line(self, v, meta, cp, wiped, extra=""):
        return (
            f"法官（{meta.get('model') or '配的模型'}"
            + (
                f"，{float(meta.get('ms') or 0) / 1000:.0f} 秒"
                if meta.get("ms")
                else ""
            )
            + f"）判：{v.get('verdict')}｜{v.get('kind')}｜{'（删了数据 不写）' if wiped else (v.get('reason') or '没写理由')}｜"
            f"引用 {' '.join('#' + str(n) for n in v.get('cites') or []) or '无'} → 代码核实 {cp.get('n', 0)} 句{extra}"
        )

    UNDO = "要撤：/法庭 撤销 {uid}（或者面板「拉黑庭」）。撤销 = 解除冷却 / 永久不理、清掉「冷却过」，之前的线索、案子不再算；删掉的好友要他重新加；清掉的数据回不来"

    async def _tell(self, cid, conv, uid, name, why, t0, until, out, by):
        """结案以后（锁外）：给模型留一行（他下次找机器人时看到；只写事实）+ 私聊告诉管理员"""
        out = out or {}
        res, rwhy, v, ok, cp, meta = (
            out.get("result"),
            out.get("why") or "",
            out.get("v"),
            out.get("ok") or [],
            out.get("cp") or {},
            out.get("meta") or {},
        )
        stage = out.get("stage") or STAGE1
        wiped = bool(out.get("wiped"))
        if wiped:
            name, why = "", WIPED
        till = out.get("until") or until
        head = self._head(by, t0, why)
        vd = (v or {}).get("verdict") or ""
        rs = reason_for_model((v or {}).get("reason"))
        rsx = f"（{rs}）" if rs else ""
        if not wiped:
            if res == "撤销了":
                self._note(uid, head + "管理员撤销了：不冷却了")
            elif res == "没判成":
                self._note(
                    uid,
                    head
                    + f"系统让你们冷却到 {md(till)}；法官没判成（{rwhy}）：只冷却 不升级",
                )
            elif res == "放过":
                self._note(
                    uid,
                    head
                    + (
                        f"法官判{vd}{rsx}：不冷却"
                        if vd in ("放过", "保留")
                        else f"法官判{vd}，但核对下来他冲你说的话不够：放过 不冷却"
                    ),
                )
            elif res == "冷却":
                self._note(
                    uid,
                    head
                    + f"法官判{vd}{rsx}"
                    + ("，核对下来不够删除" if vd == "删除" else "")
                    + f"：你们冷却到 {md(till)}",
                )
        if not self.on("notify"):
            return
        lines = [
            f"【法庭·拉黑庭 {cid}·{stage}】{self._who(name, uid)}",
            f"{by} {md(t0)} 写的原因：{why}（先不回他、等法官判）",
        ]
        if v:
            lines.append(self._judge_line(v, meta, cp, wiped))
        lines.append(
            f"结果：{self._did(res, rwhy, out, till)}"
            + (
                f"（降级：{rwhy}）"
                if rwhy and res in ("放过", "冷却") and v and not out.get("broken")
                else ""
            )
            + (f"（{rwhy}）" if out.get("broken") else "")
        )
        if out.get("judged"):
            lines.append(f"他被判过 {out['judged']} 次（冷却 / 删除都算）")
        if ok and not wiped:
            lines.append(
                "被引用的原话："
                + "；".join(
                    f"#{m.get('n')} {md(m.get('t'))}「{_clip(m.get('text'), 40)}」"
                    for m in ok[:4]
                )
            )
        if res != "撤销了":
            lines.append(self.UNDO.format(uid=uid))
        await self.io.notify("\n".join(lines))

    # ───────── 冷却中：收证据 → 去抖 → 再审 ─────────
    def evidence(self, uid, item, now=None):
        """审理中（占位）/ 冷却中的人冲机器人说的一句 → 收进案子（只当证据）→ 收了没有；收了 → 冷却中的案子去抖再审
        item：{t, mid, conv, tag, text}；认 AI 认出的机器人、白名单（案子不收证据）、删过数据的案子 → 不收"""
        if not self.on("enabled") or not self.on("retrial"):
            return False
        uid = str(uid or "")
        st = self.store
        if not st.cooling(uid, now) or st.is_blocked_forever(uid):
            return False
        c = st.ev_case(uid, now)
        if not c or self.io.is_ai(uid):
            return False
        if not st.add_ev(c["id"], item, now):
            return False
        self.log(
            f"「{c.get('name') or uid}」在{'审理' if c.get('state') == '审理中' else '冷却'}中冲机器人说的话 → 只当证据收进案子 {c['id']}"
            f"（{item.get('tag')}；机器人看不到、不回）"
        )
        if c.get("state") == "冷却中":
            self._rt_kick(c["id"])
        return True

    def _rt_kick(self, cid):
        """冷却中有了没审过的证据 → 这个案子的再审去抖任务（一个案子一个；已经在等的不另起）"""
        t = self.tasks.get(cid)
        if t is not None and not t.done():
            return t
        t = self._spawn(self._rt_watch(cid))
        if t is not None:
            self.tasks[cid] = t
        if len(self.tasks) > 200:
            for k in [k for k, x in self.tasks.items() if x.done()]:
                self.tasks.pop(k, None)
        return t

    async def _rt_watch(self, cid):
        """再审的去抖：最后一句之后安静满 retrial_quiet_sec 秒 → 再审一次；又说了 → 接着等；
        冷却到点 / 撤销了 / 删了数据 / 再审用完 / 已经永久不理 / 再审关了 → 不再审（冷却到点的顺手结案）"""
        st = self.store
        while True:
            c = st.case(cid) or {}
            uid = str(c.get("uid") or "")
            now = self.clock()
            if (
                not c
                or c.get("wiped")
                or c.get("state") != "冷却中"
                or not uid
                or st.is_blocked_forever(uid)
            ):
                return
            if not self.on("enabled") or not self.on("retrial"):
                return
            cu = st.cooling(uid, now)
            if not cu or float(c.get("cool_until") or 0) <= now:
                st.sweep(now)
                return
            if len(c.get("retrials") or []) >= max(0, int(st.f("retrial_max"))):
                return
            if not st.ev_due(c):
                return
            wait = float(c.get("ev_last") or 0) + st.f("retrial_quiet_sec") - now
            if wait > 0:
                await asyncio.sleep(min(wait, max(0.05, cu - now)) + 0.05)
                continue
            if not await self._retrial(cid):
                return

    def _rt_dossier(self, c, now):
        uid = str(c.get("uid"))
        where = where_map()
        where(c.get("conv"))
        ev = [
            dict(x, uid=uid, where=where(str(x.get("conv") or "")))
            for x in c.get("ev") or []
            if isinstance(x, dict)
        ]
        return {
            "stage": STAGE_RE,
            "uid": uid,
            "name": c.get("name") or "",
            "close": False,
            "ai": False,
            "now": now,
            "limit": EV_MAX,
            "ev": ev,
            "orig": {
                "t": c.get("t"),
                "why": c.get("why"),
                "by": c.get("by") or "机器人",
                "stage": c.get("stage") or STAGE1,
                "verdict": c.get("verdict"),
                "reason": c.get("reason"),
                "quotes": c.get("quotes") or [],
                "until": c.get("cool_until"),
            },
        }

    async def _retrial(self, cid):
        """再审一次：卷宗 = 立案时的情况 + 冷却期间他冲机器人说的话（单独编号）→ 法官判 删除 / 不动 →
        锁里：核对引用（冷却期间至少 1 句、加上立案时核实过的一共 ≥ retrial_min_total；像道歉的不算）→ 删除 / 不动
        没判成 = 不动；删除（或者撤销了 / 已经永久不理）才告诉管理员 → 这次开审了没有"""
        st = self.store
        now = self.clock()
        c = st.retrial_begin(cid, now)
        if not c:
            return False
        uid, k = str(c.get("uid")), len(c.get("retrials") or []) + 1
        v, fail, meta = None, "", {}
        try:
            v = await self._judge(
                self._rt_dossier(c, now), c.get("conv") or "", uid, meta
            )
            if not v.get("ok"):
                fail = v.get("why") or "法官没判成"
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            fail = "法官超时"
        except Exception as e:
            fail = f"法官出错（{type(e).__name__}）"
        out = None
        async with self.lock:
            try:
                out = await self._rt_exec(cid, c, k, v, fail, meta)
            except Exception as e:
                self.log(f"再审执行出错：{e!r}"[:150])
                st.retrial_end(
                    cid,
                    {
                        "t": int(self.clock()),
                        "k": k,
                        "verdict": "",
                        "result": "不动",
                        "why": f"执行出错（{type(e).__name__}）",
                    },
                )
        if out and out.get("result") not in ("不动",):
            try:
                await self._rt_tell(cid, c, k, out)
            except Exception as e:
                self.log(f"再审告诉管理员出错：{e!r}"[:120])
        return True

    async def _rt_exec(self, cid, c, k, v, fail, meta):
        st = self.store
        now = self.clock()
        uid, conv = str(c.get("uid")), str(c.get("conv") or "")
        t0 = float(c.get("t") or 0)
        name, why, by = c.get("name") or "", c.get("why") or "", c.get("by") or "机器人"
        table = (v or {}).get("table") or {}
        ok, cp, extra = [], {"n": 0, "days": 0, "why": ""}, {}
        wiped = bool((st.case(cid) or {}).get("wiped"))
        if wiped:
            name, why = "", WIPED
        if st.pardoned_at(uid) >= t0:
            res, rwhy = "撤销了", "管理员撤销了"
        elif st.is_blocked_forever(uid):
            res, rwhy = "已经在名单里", ""
        elif not st.cooling(uid, now):
            res, rwhy = "不动", "冷却已经到点了"
        elif fail:
            res, rwhy = "不动", f"法官没判成（{fail}）"
        else:
            ok = st.verify(v.get("cites"), table, uid, now)
            cp = st.trial_caps(
                STAGE_RE, v.get("verdict"), ok, prior=int(c.get("n_ok") or 0)
            )
            ok = cp.get("ok", ok)
            res, rwhy = cp["result"], cp["why"]
        friend = None
        if res == "删除":  # 先查好友（要 await）—— 保护、每天上限都在查完以后看
            friend = await self._friend_of(uid)
            if not wiped and (st.case(cid) or {}).get("wiped"):
                wiped, name, why = True, "", WIPED
            pw = self._keep_safe(uid)
            if pw:
                res, rwhy, ok = "不动", pw, []
        if res == "删除":
            dn, daily = st.deleted_today(now), max(0, int(st.f("delete_daily")))
            if dn >= daily:
                res, rwhy = (
                    "不动",
                    f"今天已经删除了 {dn} 个（每天最多 {daily} 个）：照旧冷却",
                )
        if res == "删除":
            extra = await self._remove(
                cid, conv, uid, name, why, t0, ok, now, None, by, friend
            )
            res, rwhy = extra.get("result") or res, extra.get("why") or rwhy
            if extra.get("wiped"):
                wiped, name, why, ok = True, "", WIPED, []
            if res == "冷却":  # 名单写不进去：照旧冷却（再审里等于不动）
                res = "不动"
        vd = (v or {}).get("verdict", "") if (v and not fail) else ""
        rec = {
            "t": int(now),
            "k": k,
            "verdict": vd,
            "kind": (v or {}).get("kind", "") if vd else "",
            "reason": "" if wiped else ((v or {}).get("reason", "") if vd else ""),
            "cites": list((v or {}).get("cites") or []) if vd else [],
            "n_ok": cp.get("n", 0),
            "result": res,
            "why": rwhy,
            "model": meta.get("model", ""),
            "ms": meta.get("ms") or (v or {}).get("ms") or 0,
        }
        for f in ("friend", "wipe"):
            if extra.get(f):
                rec[f] = extra[f]
        fields = {}
        if res == "删除":
            fields = {
                "state": "结案",
                "result": res,
                "end": f"再审第 {k} 次判删除",
                "closed": int(now),
            }
        st.retrial_end(cid, rec, **fields)
        self.log(
            f"案子 {cid} 再审第 {k} 次{self._who(name, uid)}："
            + (
                f"法官判 {vd}（{rec['kind']}：{rec['reason']}）引用 {rec['cites']} 核实 {cp.get('n', 0)} 句 → "
                if vd
                else ""
            )
            + res
            + (f"（{rwhy}）" if rwhy else "")
        )
        return {
            "result": res,
            "why": rwhy,
            "v": v if vd else None,
            "ok": [] if wiped else ok,
            "cp": cp,
            "meta": meta,
            "wiped": wiped,
            "judged": extra.get("judged") or 0,
            "friend": extra.get("friend", ""),
            "wipe": extra.get("wipe", ""),
            "n_ev": len(c.get("ev") or []),
        }

    async def _rt_tell(self, cid, c, k, out):
        """再审判了删除（或者撤销了 / 已经永久不理）→ 私聊告诉管理员"""
        res, rwhy, v, ok, cp, meta = (
            out.get("result"),
            out.get("why") or "",
            out.get("v"),
            out.get("ok") or [],
            out.get("cp") or {},
            out.get("meta") or {},
        )
        uid = str(c.get("uid"))
        wiped = bool(out.get("wiped"))
        name, why = ("", WIPED) if wiped else (c.get("name") or "", c.get("why") or "")
        if not self.on("notify"):
            return
        did = {
            "删除": f"删除：永久不理他（所有群和私聊都不理、他再加好友自动拒绝）；删好友：{out.get('friend') or '—'}；清数据：{out.get('wipe') or '—'}",
            "撤销了": f"你撤销了 → 不执行（{rwhy}）",
            "已经在名单里": "他已经在永久不理的名单里了 → 不再执行",
        }.get(res, res)
        lines = [
            f"【法庭·拉黑庭 {cid}·再审第 {k} 次】{self._who(name, uid)}",
            f"{c.get('by') or '机器人'} {md(c.get('t'))} 写的原因：{why}；冷却到 {md(c.get('cool_until'))}，"
            f"这期间他冲机器人说了 {out.get('n_ev') or 0} 句（机器人看不到、没回），说完安静了 → 再审",
        ]
        if v:
            lines.append(
                self._judge_line(
                    v,
                    meta,
                    cp,
                    wiped,
                    f"（冷却期间）+ 立案时 {int(c.get('n_ok') or 0)} 句",
                )
            )
        lines.append(f"结果：{did}")
        if out.get("judged"):
            lines.append(f"他被判过 {out['judged']} 次（冷却 / 删除都算）")
        if ok and not wiped:
            lines.append(
                "被引用的原话（冷却期间）："
                + "；".join(
                    f"#{m.get('n')} {md(m.get('t'))}「{_clip(m.get('text'), 40)}」"
                    for m in ok[:4]
                )
            )
        if res != "撤销了":
            lines.append(self.UNDO.format(uid=uid))
        await self.io.notify("\n".join(lines))

    # ───────── 重启收尾 ─────────
    def boot_now(self):
        """一开机马上做（不等平台连上）：卡在「审理中」的（上次庭审到一半重启了）先把冷却写上 —— 占位到点以后他就又被回了；
        庭审期间被撤销的 / 重启前已经执行了的不碰（boot 里收尾）→ 写了几个"""
        st = self.store
        n = 0
        for c in st.stuck_cases():
            uid = str(c.get("uid") or "")
            if float(c.get("t") or 0) >= self.start_ts or not uid:
                continue
            if (st.blocked_list().get(uid) or {}).get("case") == c.get("id"):
                continue
            if c.get("pardoned") or st.pardoned_at(uid) >= float(c.get("t") or 0):
                continue
            if st.start_cool(
                uid,
                c.get("name") or "",
                c.get("why") or "",
                c.get("conv") or "",
                now=float(c.get("t") or self.clock()),
                case=c.get("id"),
                by=c.get("by") or "机器人",
            ):
                n += 1
        return n

    async def boot(self, wait=0.0):
        """开机收尾（平台连上以后一次）：
        ① 真删好友进行中的（调用期间重启了）→ 查一次好友列表，记 删了 / 没删 / 不确定
        ② 卡在「审理中」的：重启前已经执行了 → 照实记；庭审期间撤销了 → 结案、不冷却；没执行 → 没判成：冷却（从立案那一刻算）、不升级
        ③ 冷却中的案子：再审到一半重启了 → 那次按不动记；冷却到点了 → 结案；还有没审的证据 → 接着等他说完再审（证据不丢）
        ①② 有的话私聊告诉管理员一次；只管这次开机之前的 → 收尾了几件"""
        if wait:
            await asyncio.sleep(wait)
        st = self.store
        told, done = [], {}
        for p in st.pending_open():
            if float(p.get("t") or 0) >= self.start_ts:
                continue
            try:
                fs = await self.io.is_friend(p.get("uid"))
            except Exception:
                fs = None
            res = "删了" if fs is False else ("没删" if fs is True else "不确定")
            st.pending_end(p.get("uid"), p.get("day"), res, "重启后核实")
            if p.get("case"):
                done[str(p["case"])] = res
                st.note_delete_update(p["case"], friend=res)
            told.append(
                f"删 {p.get('uid')} 好友的时候插件重启了 → 开机查了好友列表：{res}"
            )
        for c in st.stuck_cases():
            if float(c.get("t") or 0) >= self.start_ts:
                continue
            uid = str(c.get("uid") or "")
            e = st.blocked_list().get(uid) or {}
            if e.get("case") == c.get("id") or str(c.get("id")) in done:
                st.close_case(
                    c["id"],
                    result="删除",
                    result_why="重启前已经执行了（结案的记录没写完，开机补上）",
                )
                told.append(
                    f"案子 {c.get('id')}「{c.get('name')}」({uid})：重启前已经执行了 → 删除（记录开机补上了）"
                )
                continue
            if c.get("pardoned") or st.pardoned_at(uid) >= float(c.get("t") or 0):
                st.close_case(
                    c["id"],
                    result="撤销了",
                    result_why="管理员撤销了（庭审到一半插件重启了）",
                    ev=[],
                )
                self.log(
                    f"开机：案子 {c.get('id')} 庭审期间管理员撤销了 → 结案、不冷却"
                )
                continue
            u2 = (
                st.start_cool(
                    uid,
                    c.get("name") or "",
                    c.get("why") or "",
                    c.get("conv") or "",
                    now=float(c.get("t") or self.clock()),
                    case=c.get("id"),
                    by=c.get("by") or "机器人",
                )
                or 0
            )
            st.close_case(
                c["id"],
                result="没判成",
                result_why="重启（庭审到一半插件重启了）",
                cool_until=int(u2 or c.get("cool_until") or 0),
                ev=[],
            )
            if u2 > self.clock():
                self._note(
                    uid,
                    self._head(
                        c.get("by") or "机器人", c.get("t"), c.get("why") or "没写原因"
                    )
                    + f"系统让你们冷却到 {md(u2)}；法官没判成（插件重启了）：只冷却 不升级",
                )
            self.log(
                f"开机：案子 {c.get('id')}（{c.get('stage') or STAGE1}）上次没审完 → 没判成（重启），冷却到 {md(u2) or '（已经到点）'}"
            )
            told.append(
                f"案子 {c.get('id')}「{c.get('name')}」({uid})：庭审到一半插件重启了 → 没判成，不升级（冷却到 {md(u2) or '已经到点'}）"
            )
        st.sweep()
        for c in st.cooling_cases():
            rr = c.get("rt_run")
            if rr and float(rr) < self.start_ts:
                st.retrial_end(
                    c["id"],
                    {
                        "t": int(self.clock()),
                        "k": len(c.get("retrials") or []) + 1,
                        "verdict": "",
                        "result": "不动",
                        "why": "重启（再审到一半插件重启了）",
                    },
                )
                self.log(
                    f"开机：案子 {c.get('id')} 上次再审到一半插件重启了 → 这次再审按不动记"
                )
            c2 = st.case(c["id"]) or {}
            if self.on("retrial") and st.ev_due(c2):
                self._rt_kick(c["id"])
        if told and self.on("notify"):
            await self.io.notify("【法庭·拉黑庭】开机收尾：\n" + "\n".join(told[:8]))
        return len(told)

    # ───────── 撤销 / 申诉 / 删数据 ─────────
    def unblock(self, uid, by="管理员"):
        """撤销（管理员指令 / 面板）：解除冷却 / 永久不理、清掉「冷却过」、记撤销时间 → 撤掉的那一项 / None
        正在等的再审去抖自己会看到案子结了、不再审；正在审的那一次执行前会看到撤销了、不执行"""
        uid = re.sub(r"\D", "", str(uid or ""))
        e = self.store.unblock(uid, by)
        if e is not None:
            self.log(
                f"{by}撤销了 {uid} 的{e.get('what')}（解除冷却 / 永久不理、清掉冷却过，之前的不再算）"
            )
            self.store.take_notes(
                uid
            )  # 以前留给模型的那几行（法官判冷却…）作废，换成撤销这一行
            self._note(
                uid, f"管理员 {md(self.clock())} 撤销了对他的{e.get('what')}：照常回他"
            )
        return e

    def state_line(self, uid, now=None):
        """申诉 / 面板：他现在是什么情况（一行）"""
        st = self.store
        uid = str(uid)
        g = st.gate(uid, now)
        if g == "blocked":
            e = st.blocked_list().get(uid) or {}
            c = st.case(str(e.get("case") or "")) or {}
            return (
                f"永久不理（{md(e.get('t'))} 起；案子 {e.get('case') or '—'}·{c.get('stage') or '—'}：法官判 {c.get('verdict') or '—'}"
                + (f"（{_clip(c.get('reason'), 40)}）" if c.get("reason") else "")
                + f"；写的原因：{e.get('why') or '—'}）",
                str(e.get("case") or ""),
            )
        if g in ("hold", "cool"):
            c0 = st.cool_list(now).get(uid) or {}
            c = st.case(str(c0.get("case") or "")) or {}
            if g == "hold":
                return (
                    f"审理中（法官还没判；案子 {c0.get('case') or '—'}；写的原因：{c0.get('why') or '—'}）",
                    str(c0.get("case") or ""),
                )
            return (
                f"冷却到 {md(c0.get('until'))}（案子 {c0.get('case') or '—'}·{c.get('stage') or '—'}：法官判 {c.get('verdict') or '—'}"
                + (f"（{_clip(c.get('reason'), 40)}）" if c.get("reason") else "")
                + f"；写的原因：{c0.get('why') or '—'}）",
                str(c0.get("case") or ""),
            )
        return "", ""

    async def appeal(self, uid, name, text, src="指令", now=None):
        """被冷却 / 永久不理的人申诉 → 私聊转给管理员（带案子情况）→ {"ok", "why"}
        法庭不审申诉；同一个案子只转一次、同一个人 appeal_days 天里最多转一次；申诉的这句不当证据（不进再审）"""
        if not self.on("enabled") or not self.on("appeal"):
            return {"ok": False, "why": "申诉关着"}
        uid = str(uid or "")
        st = self.store
        line, cid = self.state_line(uid, now)
        if not line:
            return {"ok": False, "why": "他现在没被冷却、也不是永久不理的人"}
        okk, why = st.appeal_check(uid, cid, now)
        if not okk:
            self.log(f"{self._who(name, uid)}申诉：{why} → 这次不转")
            return {"ok": False, "why": why}
        text = _clip(text, 200)
        st.add_appeal(uid, cid, text, src, now, name=name, state=line[:60])
        self.log(
            f"{self._who(name, uid)}申诉（{src}）→ 转给管理员：「{_clip(text, 40)}」"
        )
        if self.on("notify"):
            await self.io.notify(
                "\n".join(
                    [
                        f"【法庭·申诉】{self._who(name, uid)}（{src}）：「{text or '（没写理由）'}」",
                        f"他现在：{line}",
                        f"要撤：/法庭 撤销 {uid}（或者面板「拉黑庭」）。法庭不审申诉；他收不到机器人的回话"
                        + (
                            "（只收到一条「已转给管理员」的系统提示）"
                            if self.on("appeal_tip")
                            else ""
                        ),
                    ]
                )
            )
        return {"ok": True, "why": ""}

    def forget(self, uid, names=()):
        """删数据（拉黑庭这一半）：名单 / 冷却那一项留着（名字、原因、原话抹掉）、记的聊天删掉 → 动了几条"""
        uid = str(uid or "")
        if not uid:
            return 0
        n = self.store.forget(uid, set(names or ()) | set(self.msglog.names_of(uid)))
        n += self.msglog.forget(uid, names)
        return n
