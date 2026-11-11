"""主流程：采集 → 规则 → 判定 → 分级 → 发布。用法：python -m watch run"""
import sys

import yaml

from . import common, judge, publish, sources


def load_config():
    return yaml.safe_load((common.ROOT / "config" / "signals.yaml").read_text(encoding="utf-8"))


def new_alert(alerts, kind, title, body, *, url="", key, track="", hyp=""):
    """key 用来去重：同一件事只报一次。"""
    aid = f"{kind}:{key}"
    if any(a["id"] == aid for a in alerts):
        return False
    alerts.insert(0, {"id": aid, "kind": kind, "title": f"【{publish.KIND_LABEL[kind]}】{title}", "body": body,
                      "url": url, "track": track, "hyp": hyp, "time": common.now().isoformat()})
    return True


def route(verdicts, cfg, state, alerts):
    """把模型返回的概率分成三级：警报 / 月度摘要 / 丢弃。"""
    th = cfg["thresholds"]
    hyps = {h["id"]: h for h in cfg["hypotheses"]}
    week = state.setdefault("alerts_per_week", {})
    wk = common.week_key()
    for v in verdicts:
        item, ans = v["item"], v["answers"]
        solid = ans.get("substantive", 0) >= th["substantive"]
        evidence = []
        for key, p in ans.items():
            if key == "substantive" or p < th["digest"]:
                continue
            if key == "actionable":
                kind, hid, label = "actionable", "", "现在就能用上"
            else:
                kind, hid = ("confirm" if key[0] == "c" else "refute"), key[2:]
                if hid not in hyps:
                    continue
                label = f"{hid} {hyps[hid]['label']}"
            strong = p >= th["alert"] and solid
            if strong and week.get(wk, 0) >= cfg.get("max_alerts_per_week", 7):
                strong = False
                print(f"[route] 本周警报已到上限, 降级为摘要: {item['id']}")
            if strong:
                body = f"{label}。概率 {p:.2f}（{v['provider']}）。来源 {item['source']}。{item['summary'][:300]}"
                if new_alert(alerts, kind, item["title"], body, url=item["url"], key=f"{hid}:{item['id']}",
                             track=item["track"], hyp=hid):
                    week[wk] = week.get(wk, 0) + 1
            evidence.append({"hyp": hid, "kind": kind, "p": round(p, 3), "level": "strong" if strong else "weak"})
        v["evidence"] = evidence
    common.append_jsonl(common.DATA / "verdicts" / f"{common.month_key()}.jsonl", verdicts)


def check_metrics(cfg, state, alerts):
    base = state.setdefault("metric_baseline", {})
    rows = []
    for m in cfg.get("metrics", []):
        try:
            val = sources.fetch_metric(m)
            state["health"]["metric:" + m["id"]] = 0
        except Exception as e:
            state["health"]["metric:" + m["id"]] = state["health"].get("metric:" + m["id"], 0) + 1
            print(f"[metric] {m['id']}: 失败 ({e})")
            continue
        rows.append({"id": m["id"], "value": val, "time": common.now().isoformat()})
        b = base.setdefault(m["id"], val)
        f = m.get("factor", 2.0)
        if b and (val >= b * f or val <= b / f):
            word = "升到" if val > b else "降到"
            new_alert(alerts, "metric", f"{m['label']} 周下载量{word}基线的 {val / b:.1f} 倍",
                      f"基线 {b:,}，现在 {val:,}。基线已重置为当前值。", key=f"{m['id']}:{common.now():%Y-%m-%d}",
                      track=m.get("track", ""))
            base[m["id"]] = val
    common.append_jsonl(common.DATA / "metrics.jsonl", rows)


def check_health(cfg, state, alerts, judge_status):
    n = cfg.get("health_fail_runs", 3)
    for sid, fails in state["health"].items():
        if fails == n:  # 只在刚好到阈值那次报一次
            new_alert(alerts, "health", f"数据源 {sid} 连续 {n} 次失败",
                      "这个源暂时没有数据进来。恢复后自动继续，无需操作；长期失败请检查 config/signals.yaml。",
                      key=f"{sid}:{common.now():%Y-%m-%d}")
    jf = state.get("judge_fail_runs", 0)
    jf = jf + 1 if judge_status == "failed" else 0
    state["judge_fail_runs"] = jf
    if jf == n:
        new_alert(alerts, "health", f"判定层连续 {n} 次失败",
                  "模型接口调用失败（密钥失效、余额不足或接口变更）。候选条目留在队列里，恢复后继续判。",
                  key=f"judge:{common.now():%Y-%m-%d}")


def tally(months=None):
    """从判定日志统计每条判断的证据条数。"""
    out = {}
    for path in sorted((common.DATA / "verdicts").glob("*.jsonl")):
        if months and path.stem not in months:
            continue
        for v in common.read_jsonl(path):
            for e in v.get("evidence", []):
                if e["hyp"]:
                    t = out.setdefault(e["hyp"], {})
                    k = f"{e['kind']}_{e['level']}"
                    t[k] = t.get(k, 0) + 1
    return out


def tally_text(cfg, t):
    lines = []
    for h in cfg["hypotheses"]:
        x = t.get(h["id"], {})
        lines.append(f"{h['id']} {h['label']}：证实 强{x.get('confirm_strong', 0)}/弱{x.get('confirm_weak', 0)}，"
                     f"反向 强{x.get('refute_strong', 0)}/弱{x.get('refute_weak', 0)}")
    return "；".join(lines)


def monthly_digest(cfg, state, digests):
    cur = common.month_key()
    last = state.get("last_digest_month")
    state["last_digest_month"] = cur
    if not last or last == cur:
        return
    weak = []
    for v in common.read_jsonl(common.DATA / "verdicts" / f"{last}.jsonl"):
        if any(e["level"] == "weak" for e in v.get("evidence", [])):
            tags = ",".join(f"{e['hyp'] or e['kind']}:{e['p']:.2f}" for e in v["evidence"])
            weak.append(f"{v['item']['title']} [{tags}] {v['item']['url']}")
    unjudged = len(state.get("pending", []))
    usage = {k: v for k, v in state.get("usage", {}).items()}
    calls = sum(u.get("jev_calls", 0) for u in usage.values())
    toks = sum(u.get("jev_input_tokens", 0) for u in usage.values())
    oc = sum(u.get("openai_calls", 0) for u in usage.values())
    body = (f"【台账】{tally_text(cfg, tally([last]))}。"
            f"【弱证据 {len(weak)} 条】" + " | ".join(weak[:40]) +
            f"。【累计用量】Jev {calls} 次 / {toks} token（约 ${toks * cfg['judge'].get('jev_usd_per_mtok', 0.042) / 1e6:.4f}），"
            f"OpenAI {oc} 次。待判队列 {unjudged} 条。")
    digests.insert(0, {"id": f"digest:{last}", "title": f"{last} 月度摘要", "body": body, "url": "",
                       "time": common.now().isoformat()})


def reconcile(cfg, state, alerts):
    done = state.setdefault("reconciled", [])
    today = common.now().strftime("%Y-%m-%d")
    for d in cfg.get("reconcile_dates", []):
        if d <= today and d not in done:
            done.append(d)
            new_alert(alerts, "reconcile", f"{d} 判断对账", "各条判断到今天的证据累计：" + tally_text(cfg, tally()),
                      key=d)


def run():
    cfg = load_config()
    state = common.load_json(common.DATA / "state.json", {})
    alerts = common.load_json(common.DATA / "alerts.json", [])
    digests = common.load_json(common.DATA / "digests.json", [])
    src_by_id = {s["id"]: s for s in cfg["sources"]}

    fresh = sources.collect(cfg, state)
    common.append_jsonl(common.DATA / "items" / f"{common.month_key()}.jsonl", fresh)
    cands = [it for it in fresh if sources.passes_rules(it, src_by_id[it["source"]], cfg)]
    print(f"[rules] 新条目 {len(fresh)} 条, 通过规则 {len(cands)} 条")
    pending = state.setdefault("pending", [])
    pending.extend(cands)
    if len(pending) > cfg.get("max_pending", 300):
        print(f"[rules] 待判队列超上限, 丢弃最旧的 {len(pending) - cfg['max_pending']} 条")
        del pending[:-cfg["max_pending"]]

    verdicts, status = judge.judge_pending(cfg, state)
    route(verdicts, cfg, state, alerts)
    check_metrics(cfg, state, alerts)
    check_health(cfg, state, alerts, status)
    monthly_digest(cfg, state, digests)
    reconcile(cfg, state, alerts)

    for k in sorted(state.get("usage", {}))[:-26]:  # 用量只留半年
        del state["usage"][k]
    state["last_run"] = common.now().isoformat()
    del alerts[300:]
    del digests[60:]
    common.save_json(common.DATA / "alerts.json", alerts)
    common.save_json(common.DATA / "digests.json", digests)
    publish.publish(cfg, state, alerts, digests, tally())
    common.save_json(common.DATA / "state.json", state)
    print(f"[done] 警报累计 {len(alerts)} 条, 判定状态 {status}")


def selftest():
    """用一条固定的假条目真实调用一次判定接口，只打印是否成功，用来验证密钥。"""
    cfg = load_config()
    item = {"id": "selftest", "source": "selftest", "track": "agents", "published": common.now().isoformat(),
            "title": "Example Corp ships permissioned agent-to-agent calls across organizations",
            "summary": "Self-test item. Not a real event."}
    provs = judge.providers()
    if not provs:
        print("[selftest] 没有配置任何密钥")
        sys.exit(1)
    ok = True
    for name, fn in provs:
        try:
            answers, tokens, model = fn(item, judge.build_questions(item, cfg), cfg)
            print(f"[selftest] {name}: 成功, 模型 {model}, 输入 {tokens} token, 返回 {len(answers)} 个答案")
        except Exception as e:
            ok = False
            print(f"[selftest] {name}: 失败 ({e})")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    if sys.argv[1:] == ["run"]:
        run()
    elif sys.argv[1:] == ["selftest"]:
        selftest()
    else:
        print("用法: python -m watch run | selftest")
        sys.exit(2)
