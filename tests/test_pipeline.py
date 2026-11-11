"""端到端测试：假的网络 + 假的模型，跑完整流程。用法：python tests/test_pipeline.py"""
import json
import os
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
tmp = Path(tempfile.mkdtemp())
os.environ["WATCH_ROOT"] = str(tmp)
os.environ["WATCH_BACKOFF"] = "0"
os.environ["GITHUB_REPOSITORY"] = "Someone/signal-watch"
os.environ["TYPESAFE_API_KEY"] = "sk-test-SECRET"
for k in ("OPENAI_API_KEY", "OPENAI_MODEL"):
    os.environ.pop(k, None)
shutil.copytree(REPO / "config", tmp / "config")
sys.path.insert(0, str(REPO))

from watch import __main__ as main, common, judge  # noqa: E402

ATOM = '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">{}</feed>'
ENTRY = ('<entry><id>{id}</id><updated>{t}</updated><link rel="alternate" href="https://x.test/{id}"/>'
         "<title>{title}</title><content type=\"html\">&lt;p&gt;{body}&lt;/p&gt;</content></entry>")
feeds = {"mcp": [], "apple": []}
metric = {"npm": 100}
sitemap = [("/projects/goose", "2026-11-05T18:05:41Z"), ("/about", "2026-11-05T18:05:41Z"), ("/projects/", "2026-11-05T18:05:41Z")]
broken = set()
calls = []


def fake_http(url, *, body=None, headers=None, timeout=30, retries=2):
    if body is not None:  # 模型调用
        calls.append(url)
        assert url == judge.JEV_URL and headers["Authorization"] == "Bearer sk-test-SECRET"
        assert body["model"] == "jev-1.14.0" and set(body) == {"model", "state", "questions"}
        title = body["state"]["title"].lower()
        ans = {}
        for k in body["questions"]:
            p = 0.05
            if "stable" in title and k in ("substantive", "c_A1"):
                p = 0.95
            if "aaif projects" in title and k in ("substantive", "c_A5"):
                p = 0.9
            if "maybe" in title and k == "c_A2":
                p = 0.6
            if "kit" in title and k in ("substantive", "actionable"):
                p = 0.99
            ans[k] = {"type": "noul", "noul": p}
        return json.dumps({"model": "jev-1.14.0", "answers": ans, "usage": {"input_tokens": 400, "output_tokens": 9}}).encode()
    for name in broken:
        if name in url:
            raise common.HttpError(503)
    if "npmjs" in url:
        return json.dumps({"downloads": metric["npm"]}).encode()
    if "sitemap" in url:
        return ("<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>" + "".join(
            f"<url><loc>https://aaif.io{p}</loc><lastmod>{t}</lastmod></url>" for p, t in sitemap) + "</urlset>").encode()
    if "modelcontextprotocol" in url:
        return ATOM.format("".join(feeds["mcp"])).encode()
    if "releases.rss" in url:
        return ('<rss version="2.0"><channel>' + "".join(feeds["apple"]) + "</channel></rss>").encode()
    if "npmjs" in url:
        return json.dumps({"downloads": metric["npm"]}).encode()
    if "pypistats" in url:
        return json.dumps({"data": {"last_week": 50}}).encode()
    if "algolia" in url:
        return b'{"hits": []}'
    if "datatracker" in url:
        return b'{"objects": []}'
    if "api.github.com" in url:
        return b'{"items": []}'
    return ATOM.format("").encode()


common.http = fake_http


def run(day):
    os.environ["WATCH_NOW"] = day + "T00:00:00"
    main.run()
    return (common.load_json(common.DATA / "alerts.json", []), common.load_json(common.DATA / "state.json", {}))


def kinds(alerts):
    return sorted(a["kind"] for a in alerts)


# 第 1 天：首次运行只建基线，不报警、不调模型
feeds["mcp"].append(ENTRY.format(id="old", t="2026-11-01T00:00:00Z", title="old stable release", body="x"))
alerts, state = run("2026-11-12")
assert alerts == [] and calls == [], "首次运行应只建基线"

# 第 2 天：一条强证据、一条弱证据、一条带 HTML 的可上手、一条被规则挡掉的补丁版本
feeds["mcp"] += [
    ENTRY.format(id="n1", t="2026-11-12T10:00:00Z", title="2026-11 stable release", body="big"),
    ENTRY.format(id="n2", t="2026-11-12T11:00:00Z", title="maybe identity", body="hmm"),
    ENTRY.format(id="n3", t="2026-11-12T12:00:00Z", title="&lt;script&gt;alert(1)&lt;/script&gt; kit &amp;amp; &quot;q&quot;", body="x"),
]
feeds["apple"].append("<item><title>visionOS 27.2.1 (24N124)</title><link>https://a.test/1</link>"
                      "<guid>a1</guid><pubDate>Thu, 12 Nov 2026 10:00:00 PST</pubDate></item>")
alerts, state = run("2026-11-13")
assert kinds(alerts) == ["actionable", "confirm"], kinds(alerts)
assert len(calls) == 3, "补丁版本不应送判"
verdicts = common.read_jsonl(common.DATA / "verdicts" / "2026-11.jsonl")
assert any(e["level"] == "weak" and e["hyp"] == "A2" for v in verdicts for e in v["evidence"])

assert "hn-glasses" not in state["seen"], "停用的源不应被拉取"

# AAIF 官网出现新项目页 → 警报；已有页面改了 lastmod 不算新；重复运行不重复花钱
sitemap[0] = ("/projects/goose", "2026-11-13T10:00:00Z")
sitemap.append(("/projects/agent-identity", "2026-11-13T10:00:00.031Z"))
alerts, state = run("2026-11-14")
assert len(alerts) == 3 and len(calls) == 4 and alerts[0]["hyp"] == "A5", (len(alerts), len(calls))
alerts, state = run("2026-11-14")
assert len(alerts) == 3 and len(calls) == 4

# 指标翻倍 → 警报；数据源连续 3 次失败 → 只报一次故障
metric["npm"] = 250
broken.add("releases.rss")
for d in ("2026-11-15", "2026-11-16", "2026-11-17", "2026-11-18"):
    alerts, state = run(d)
assert kinds(alerts) == ["actionable", "confirm", "confirm", "health", "metric"], kinds(alerts)
broken.clear()

# 周调用上限：到了就停，条目留在队列
cfg_path = tmp / "config" / "signals.yaml"
cfg_path.write_text(cfg_path.read_text(encoding="utf-8").replace("jev_weekly_calls: 200", "jev_weekly_calls: 1"), encoding="utf-8")
feeds["mcp"] += [ENTRY.format(id=f"c{i}", t="2026-11-23T00:00:00Z", title=f"cap {i}", body="x") for i in range(3)]
before = len(calls)
alerts, state = run("2026-11-24")
assert len(calls) - before == 1 and len(state["pending"]) == 2, (len(calls) - before, len(state["pending"]))

# 跨月 → 月度摘要；到对账日 → 对账警报
alerts, state = run("2026-12-01")
digests = common.load_json(common.DATA / "digests.json", [])
assert [d["id"] for d in digests] == ["digest:2026-11"] and "A2" in digests[0]["body"]
alerts, state = run("2027-07-01")
assert "reconcile" in kinds(alerts)

# 输出检查：RSS 合法、HTML 被转义、密钥不落盘
for name in ("alerts.xml", "digest.xml"):
    root = ET.parse(common.DOCS / name).getroot()
    assert root.tag == "rss" and root.find("channel/title") is not None
assert len(ET.parse(common.DOCS / "alerts.xml").getroot().findall("channel/item")) == len(alerts)
assert "someone.github.io/signal-watch/alerts.xml" in (common.DOCS / "alerts.xml").read_text(encoding="utf-8")
page = (common.DOCS / "index.html").read_text(encoding="utf-8")
assert "<script>" not in page and "kit &amp; &quot;q&quot;" in page
for p in tmp.rglob("*"):
    if p.is_file():
        assert "sk-test-SECRET" not in p.read_text(encoding="utf-8", errors="ignore"), p

# 没有任何密钥：不调模型，不崩
os.environ.pop("TYPESAFE_API_KEY")
before = len(calls)
alerts, state = run("2027-07-02")
assert len(calls) == before

shutil.rmtree(tmp)
print("全部通过")
