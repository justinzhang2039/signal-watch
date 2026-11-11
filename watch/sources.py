"""采集层：把各种公开源统一成 item。不花 token。

item = {id, source, track, title, url, summary, published}
"""
import datetime as dt
import email.utils
import html
import json
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET

from . import common

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def clean(text, limit=1200):
    text = html.unescape(TAG_RE.sub(" ", text or ""))
    return WS_RE.sub(" ", text).strip()[:limit]


def parse_date(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            d = email.utils.parsedate_to_datetime(s)
        except (TypeError, ValueError):
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def parse_feed(raw):
    """同时支持 Atom 和 RSS 2.0。"""
    root = ET.fromstring(raw)
    out = []
    for e in root.iter():
        if _local(e.tag) not in ("entry", "item"):
            continue
        f = {}
        link = ""
        for c in e:
            name = _local(c.tag)
            if name == "link":
                href = c.get("href")
                if href and (not link or c.get("rel") in (None, "alternate")):
                    link = href
                elif not href and c.text:
                    link = c.text.strip()
            elif name not in f:
                f[name] = "".join(c.itertext())
        ident = f.get("id") or f.get("guid") or link
        if not ident:
            continue
        out.append({
            "id": ident.strip(),
            "title": clean(f.get("title"), 300),
            "url": link,
            "summary": clean(f.get("summary") or f.get("content") or f.get("encoded") or f.get("description")),
            "published": f.get("published") or f.get("updated") or f.get("pubDate") or f.get("date") or "",
        })
    return out


def fetch_feed(src):
    return parse_feed(common.http(src["url"]))


def fetch_arxiv(src):
    q = urllib.parse.urlencode({
        "search_query": src["query"], "sortBy": "submittedDate",
        "sortOrder": "descending", "max_results": src.get("max_results", 25),
    })
    return parse_feed(common.http("https://export.arxiv.org/api/query?" + q, timeout=60))


def fetch_hn(src):
    since = int((common.now() - dt.timedelta(days=src.get("days", 14))).timestamp())
    q = urllib.parse.urlencode({
        "query": src["query"], "tags": "story", "hitsPerPage": 30,
        "restrictSearchableAttributes": "title", "typoTolerance": "false",
        "numericFilters": f"points>{src.get('min_points', 50)},created_at_i>{since}",
    })
    data = json.loads(common.http("https://hn.algolia.com/api/v1/search_by_date?" + q))
    out = []
    for h in data.get("hits", []):
        oid = str(h.get("objectID"))
        out.append({
            "id": "hn:" + oid,
            "title": clean(h.get("title"), 300),
            "url": h.get("url") or f"https://news.ycombinator.com/item?id={oid}",
            "summary": clean(h.get("story_text"), 800),
            "published": h.get("created_at") or "",
        })
    return out


def fetch_ietf(src):
    q = urllib.parse.urlencode({
        "name__contains": src["name_contains"], "type": "draft",
        "order_by": "-id", "limit": src.get("limit", 30), "format": "json",
    })
    data = json.loads(common.http("https://datatracker.ietf.org/api/v1/doc/document/?" + q, timeout=60))
    out = []
    for o in data.get("objects", []):
        out.append({
            "id": f"ietf:{o.get('name')}-{o.get('rev')}",
            "title": clean(o.get("title"), 300) + f" ({o.get('name')}-{o.get('rev')})",
            "url": f"https://datatracker.ietf.org/doc/{o.get('name')}/",
            "summary": clean(o.get("abstract")),
            "published": o.get("time") or "",
        })
    return out


def fetch_gh_search(src):
    since = (common.now() - dt.timedelta(days=src.get("days", 30))).strftime("%Y-%m-%d")
    q = urllib.parse.urlencode({
        "q": f"{src['query']} created:>{since} stars:>={src.get('min_stars', 50)}",
        "sort": "stars", "order": "desc", "per_page": 20,
    })
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.loads(common.http("https://api.github.com/search/repositories?" + q, headers=headers))
    out = []
    for r in data.get("items", []):
        out.append({
            "id": "ghrepo:" + r.get("full_name", ""),
            "title": f"{r.get('full_name')} ★{r.get('stargazers_count')}",
            "url": r.get("html_url") or "",
            "summary": clean(r.get("description")),
            "published": r.get("created_at") or "",
        })
    return out


def fetch_sitemap(src):
    """没有 RSS 的站点：把 sitemap 里指定路径下新出现的网址当作新条目。"""
    root = ET.fromstring(common.http(src["url"], timeout=60))
    out = []
    for u in root.iter():
        if _local(u.tag) != "url":
            continue
        f = {_local(c.tag): (c.text or "").strip() for c in u}
        loc = f.get("loc", "")
        path = "/" + loc.split("://", 1)[-1].partition("/")[2]
        prefix = next((p for p in src["prefixes"] if path.startswith(p) and len(path) > len(p)), None)
        if not prefix:
            continue
        slug = path[len(prefix):].strip("/").replace("-", " ")
        out.append({"id": loc, "title": clean(f"{src.get('label', src['id'])} {prefix.strip('/')}: {slug}", 300),
                    "url": loc, "summary": "", "published": f.get("lastmod", "")})
    return out


FETCHERS = {"sitemap": fetch_sitemap, "feed": fetch_feed, "arxiv": fetch_arxiv, "hn": fetch_hn, "ietf": fetch_ietf, "gh_search": fetch_gh_search}


def fetch_metric(m):
    """返回最近一周下载量（整数）。"""
    if m["kind"] == "npm":
        pkg = urllib.parse.quote(m["package"], safe="@/")
        d = json.loads(common.http(f"https://api.npmjs.org/downloads/point/last-week/{pkg}"))
        return int(d["downloads"])
    if m["kind"] == "pypi":
        d = json.loads(common.http(f"https://pypistats.org/api/packages/{m['package']}/recent"))
        return int(d["data"]["last_week"])
    raise ValueError("unknown metric kind")


def collect(cfg, state):
    """拉所有源，返回本次新出现的 item。首次见到某个源时只建基线，不产出。"""
    seen_all = state.setdefault("seen", {})
    health = state.setdefault("health", {})
    max_age = dt.timedelta(days=cfg.get("max_age_days", 30))
    cap = cfg.get("max_new_per_source", 10)
    fresh = []
    for src in cfg["sources"]:
        sid = src["id"]
        if src.get("enabled") is False:
            health.pop(sid, None)
            continue
        try:
            raw = FETCHERS[src["type"]](src)
            health[sid] = 0
        except Exception as e:
            health[sid] = health.get(sid, 0) + 1
            print(f"[source] {sid}: 失败 ({e}), 连续 {health[sid]} 次")
            continue
        first = sid not in seen_all
        seen = seen_all.setdefault(sid, [])
        seen_set = set(seen)
        new = []
        for it in raw:
            if it["id"] in seen_set:
                continue
            seen.append(it["id"])
            seen_set.add(it["id"])
            d = parse_date(it["published"])
            if first or (d and common.now() - d > max_age):
                continue
            it.update(source=sid, track=src["track"], published=d.isoformat() if d else "")
            new.append(it)
        del seen[:-2000]
        print(f"[source] {sid}: 拉到 {len(raw)} 条, 新增 {len(new)} 条" + (" (首次, 仅建基线)" if first else ""))
        if len(new) > cap:
            print(f"[source] {sid}: 超过单源上限, 只保留前 {cap} 条")
        fresh.extend(new[:cap])
    return fresh


def passes_rules(item, src, cfg):
    """规则层：关键词筛选。include 为空表示全收。"""
    text = (item["title"] + " " + item["summary"]).lower()
    if any(re.search(p, text) for p in cfg.get("exclude", [])):
        return False
    inc = src.get("include") or []
    return not inc or any(re.search(p, text) for p in inc)
