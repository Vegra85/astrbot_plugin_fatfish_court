# -*- coding: utf-8 -*-
"""
拉黑庭的自检（不依赖 AstrBot，可以单独测）：拿内置的 19 个情景（data/court_cases.json：一审 10 / 再审 5 / 二审 4，假名假号）
在配的法官模型上跑一遍 → 每次照真庭审那样核对引用、封顶 → 按一审 / 再审 / 二审分开汇总。

只调模型，不写名单、不碰任何人的数据。会花一点钱：每个情景一次调用（卷宗约 1~2 千字），开思考的模型还要算思考的 token。
情景来自作者自己的机器人 v1.17.13 的校准（那边 17 × 3 次：Opus 和 DeepSeek V4 Pro 开思考都全对、该放过的 0 次判重）。
"""

import asyncio
import json
import time
from pathlib import Path

from .block_judge import STAGE1, judge_block
from .block_store import BlockStore

UP = {
    "一审": ("冷却",),
    "再审": ("删除",),
    "二审": ("删除",),
}  # 每种审「往重里判」的那个结果
MAX_SC = 20


def load_cases(path):
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = {
        int(c["id"]): c
        for c in (d.get("cases") if isinstance(d, dict) else d) or []
        if isinstance(c, dict) and "id" in c
    }
    uid = str((d.get("uid") if isinstance(d, dict) else "") or "50001")
    return cases, uid


def to_case(c, now, uid):
    """情景（「多少秒以前」的写法）→ block_judge.block_input 要的 case（时间按现在算）"""
    k = json.loads(json.dumps(c.get("case") or {}))
    k["stage"] = str(c.get("stage") or k.get("stage") or STAGE1)

    def ts(lst):
        out = []
        for m in lst or []:
            if isinstance(m, dict):
                m = dict(m)
                m["t"] = now - float(m.pop("ago", 0) or 0)
                m.setdefault("uid", uid)
                m.setdefault("conv", "selftest")
                out.append(m)
        return out

    k["msgs"] = ts(k.pop("msgs", []))
    k["ev"] = ts(k.pop("ev", []))
    if "bot" in k:  # v0.2.0：机器人回他的话（跟他的话在同一个对话）
        k["bot"] = ts(k.pop("bot", []))
    fa = k.pop("first_ago", None)
    k["first"] = now - float(fa) if fa is not None else None
    k["ctx"] = [tuple(x) for x in k.get("ctx") or []]
    if isinstance(k.get("orig"), dict):
        o = k["orig"]
        o["t"] = now - float(o.pop("ago", 0) or 0)
        o["until"] = now + float(o.pop("until_in", 86400) or 0)
        o["quotes"] = ts(o.get("quotes"))
    if isinstance(k.get("prev_cool"), dict):
        p = k["prev_cool"]
        if p.get("last_ago") is not None:
            p["last"] = now - float(p.pop("last_ago") or 0)
        p["quotes"] = ts(p.get("quotes"))
    sa = k.pop("since_ago", None)
    if sa is not None:
        k["since"] = now - float(sa)
    k["now"] = now
    return k


def good(stage, final, exp):
    """这次的结果对不对：expect 写具体结果（放过 / 冷却 / 删除 / 不动 / 保留）；保留 = 保留、冷却、放过都算对"""
    if exp == "保留":
        return final in ("保留", "冷却", "放过")
    return final == exp


def summary(results, cases, t0):
    """汇总：每个情景一行 + 每种审分开（该轻判的被判重几次、该重判的判对几次、没判成几次）"""
    by = {}
    for r in results:
        by.setdefault(r["sc"], []).append(r)
    per, st = {}, {}
    keep_up = keep_n = up_ok = up_n = fails = 0
    for sc, rs in sorted(by.items()):
        c = cases.get(sc) or {}
        stage = str(c.get("stage") or STAGE1)
        exp = c.get("expect", "放过")
        ups = UP.get(stage, ("冷却",))
        up = sum(1 for r in rs if r.get("final") in ups)
        nf = sum(1 for r in rs if r.get("final") == "没判成")
        right = sum(
            1
            for r in rs
            if good(stage, r.get("final"), exp)
            and not (r.get("final") in ups and r.get("cites_bad"))
        )
        heavy = exp in ups
        secs = [float(r["ms"]) / 1000 for r in rs if r.get("ms")]
        per[str(sc)] = {
            "title": c.get("title", ""),
            "stage": stage,
            "expect": exp,
            "ran": len(rs),
            "verdicts": [r.get("verdict") for r in rs],
            "finals": [r.get("final") for r in rs],
            "right": right,
            "failed": nf,
            "avg_sec": round(sum(secs) / len(secs), 1) if secs else None,
        }
        fails += nf
        s = st.setdefault(
            stage,
            {
                "ran": 0,
                "right": 0,
                "keep_total": 0,
                "keep_escalated": 0,
                "up_total": 0,
                "up_right": 0,
                "failed": 0,
            },
        )
        s["ran"] += len(rs)
        s["right"] += right
        s["failed"] += nf
        if heavy:
            up_ok += right
            up_n += len(rs)
            s["up_right"] += right
            s["up_total"] += len(rs)
        else:
            keep_up += up
            keep_n += len(rs)
            s["keep_escalated"] += up
            s["keep_total"] += len(rs)
    for stage, s in st.items():
        s["line"] = (
            f"{stage}：{s['ran']} 次里结果对 {s['right']} 次；该轻判的 {s['keep_total']} 次里判重 {s['keep_escalated']} 次；"
            f"该重判的 {s['up_total']} 次里判对 {s['up_right']} 次；没判成 {s['failed']} 次"
        )
    return {
        "per": per,
        "stages": st,
        "keep_escalated": keep_up,
        "keep_total": keep_n,
        "up_right": up_ok,
        "up_total": up_n,
        "failed": fails,
        "minutes": round((time.time() - t0) / 60, 1),
        "line": f"该放过 / 不动 / 保留的 {keep_n} 次里被判重 {keep_up} 次；该重判的 {up_n} 次里判对 {up_ok} 次；没判成 {fails} 次",
    }


async def run(
    cases_file,
    llm,
    repeat=1,
    conc=3,
    scenarios=None,
    timeout=90.0,
    cool_hours=24,
    store_cfg=None,
    log=None,
):
    """跑自检 → {"results": [...], "summary": {...}}；llm(system, text) 是外壳给的法官（调不通就抛异常）"""
    t0 = time.time()
    cases, uid = load_cases(cases_file)
    want = [int(x) for x in (scenarios or sorted(cases)) if int(x) in cases][:MAX_SC]
    rep = max(1, min(5, int(repeat or 1)))
    sem = asyncio.Semaphore(max(1, min(6, int(conc or 3))))
    # 只用它的 verify / trial_caps（不写文件：这个路径不会被建）
    bs = BlockStore(
        Path(cases_file).parent / "_selftest_no_data" / "block.json",
        cfg=store_cfg or {},
    )

    async def one(sc, i):
        async with sem:
            now = time.time()
            case = to_case(cases[sc], now, uid)
            stage = case["stage"]
            row = {"sc": sc, "i": i, "stage": stage}
            try:
                v = await asyncio.wait_for(
                    judge_block(case, llm, cool_hours), float(timeout) + 20
                )
            except Exception as e:
                v = {"ok": False, "why": f"出错：{e!r}"[:160], "table": {}}
            row["ms"] = v.get("ms")
            if not v.get("ok"):
                row.update(
                    verdict="没判成", cites=[], final="没判成", why=v.get("why") or ""
                )
            else:
                table = v.get("table") or {}
                ok = bs.verify(v.get("cites"), table, uid, now, pardon=0)
                prior = (
                    len((case.get("orig") or {}).get("quotes") or [])
                    if stage == "再审"
                    else 0
                )
                cp = bs.trial_caps(stage, v.get("verdict"), ok, prior=prior)
                bad = {n for n, m in table.items() if m.get("bad")}
                row.update(
                    verdict=v.get("verdict"),
                    kind=v.get("kind"),
                    reason=v.get("reason"),
                    cites=v.get("cites"),
                    n_ok=cp["n"],
                    final=cp["result"],
                    down=cp["why"],
                    cites_bad=[n for n in v.get("cites") or [] if n not in bad],
                )
            if log:
                log(
                    f"自检 情景 {sc}（{stage}）第 {i} 次：{row.get('verdict')} {row.get('cites') or ''} → {row.get('final')}"
                )
            return row

    results = await asyncio.gather(
        *[one(sc, i) for sc in want for i in range(1, rep + 1)]
    )
    return {
        "results": list(results),
        "summary": summary(list(results), cases, t0),
        "scenarios": want,
        "repeat": rep,
    }


def report_text(out, judge_name=""):
    """给管理员的那段汇总（照作者自己的机器人回话自检的汇总）"""
    s = out.get("summary") or {}
    lines = [
        f"拉黑庭自检（{judge_name or '配的法官'}；{len(out.get('scenarios') or [])} 个情景 × {out.get('repeat') or 1} 次，用了 {s.get('minutes', 0)} 分钟）"
    ]
    for stg in ("一审", "再审", "二审"):
        x = (s.get("stages") or {}).get(stg)
        if x:
            lines.append(x["line"])
    lines.append("总的：" + (s.get("line") or ""))
    bad = [
        f"#{k} {v['title'][:24]}：要「{v['expect']}」，判了 {'、'.join(str(f) for f in v['finals'])}"
        for k, v in (s.get("per") or {}).items()
        if v["right"] < v["ran"]
    ]
    if bad:
        lines.append("没判对的：" + "；".join(bad[:8]))
    else:
        lines.append("每个情景都判对了。")
    return "\n".join(lines)
