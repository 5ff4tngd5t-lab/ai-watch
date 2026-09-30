#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 大佬访谈监测（GitHub Actions 云版）

数据源（全部免费、无需 API key）：
  1. YouTube 频道最新视频（yt-dlp 扁平列表）
  2. Google News RSS（按人名检索过去 7 天新闻 / 访谈 / 播客）

可选能力（通过仓库 Secrets 注入环境变量）：
  LLM_API_KEY / LLM_BASE_URL / LLM_MODEL  -> 生成中文摘要与选题角度（OpenAI 兼容接口）
  PUSH_WEBHOOK                            -> 推送周报到企业微信机器人 / Server酱

产物（由 workflow 提交回仓库）：
  digest/digest-YYYY-MM-DD.md  本周素材周报
  state/seen.json              已报告条目（跨周去重）
"""

import datetime
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse

import feedparser
import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(ROOT, "state", "seen.json")
DIGEST_DIR = os.path.join(ROOT, "digest")
DAYS = 7

# ---------- 监测名单：人名 -> 标题匹配别名 ----------
PEOPLE = {
    "Sam Altman":       ["sam altman", "altman"],
    "Dario Amodei":     ["dario amodei", "amodei"],
    "Peter Thiel":      ["peter thiel", "thiel"],
    "Demis Hassabis":   ["demis hassabis", "hassabis"],
    "Jensen Huang":     ["jensen huang", "nvidia ceo"],
    "Mark Zuckerberg":  ["mark zuckerberg", "zuckerberg"],
    "Satya Nadella":    ["satya nadella", "nadella"],
    "Marc Andreessen":  ["marc andreessen", "andreessen"],
    "Elon Musk":        ["elon musk", "musk"],
    "Andrej Karpathy":  ["andrej karpathy", "karpathy"],
    "Ilya Sutskever":   ["ilya sutskever", "sutskever"],
}

# ---------- Google News 查询（每人一条，可自行调整） ----------
NEWS_QUERIES = {
    "Sam Altman":       '"Sam Altman" when:7d',
    "Dario Amodei":     '"Dario Amodei" when:7d',
    "Peter Thiel":      '"Peter Thiel" (AI OR interview OR podcast) when:7d',
    "Demis Hassabis":   '"Demis Hassabis" when:7d',
    "Jensen Huang":     '"Jensen Huang" (AI OR interview OR keynote) when:7d',
    "Mark Zuckerberg":  '"Mark Zuckerberg" (AI OR Meta OR Llama) when:7d',
    "Satya Nadella":    '"Satya Nadella" (AI OR Microsoft OR OpenAI) when:7d',
    "Marc Andreessen":  '"Marc Andreessen" (AI OR a16z) when:7d',
    "Elon Musk":        '"Elon Musk" (xAI OR Grok OR AI) when:7d',
    "Andrej Karpathy":  '"Karpathy" when:7d',
    "Ilya Sutskever":   '"Ilya Sutskever" OR "Safe Superintelligence" when:7d',
}

# ---------- YouTube 频道（节目 + 官方渠道） ----------
YOUTUBE_CHANNELS = [
    ("Dwarkesh Podcast",   "https://www.youtube.com/@DwarkeshPod/videos"),
    ("Lex Fridman Podcast","https://www.youtube.com/@lexfridman/videos"),
    ("Acquired",           "https://www.youtube.com/@AcquiredFM/videos"),
    ("All-In Podcast",     "https://www.youtube.com/@allin/videos"),
    ("a16z",               "https://www.youtube.com/@a16z/videos"),
    ("Andrej Karpathy",    "https://www.youtube.com/@AndrejKarpathy/videos"),
    ("OpenAI",             "https://www.youtube.com/@OpenAI/videos"),
    ("Anthropic",          "https://www.youtube.com/@anthropic-ai/videos"),
    ("Google DeepMind",    "https://www.youtube.com/@GoogleDeepMind/videos"),
    ("NVIDIA",             "https://www.youtube.com/@NVIDIA/videos"),
]

TRACKING_PARAMS = ("utm_", "fbclid", "gclid", "spm", "ref", "si", "feature", "ved")


def norm_url(url):
    """去掉跟踪参数，用于跨周去重。"""
    p = urllib.parse.urlsplit(url.strip())
    qs = [(k, v) for k, v in urllib.parse.parse_qsl(p.query)
          if not any(k == t or k.startswith(t) for t in TRACKING_PARAMS)]
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path.rstrip("/"),
                                    urllib.parse.urlencode(qs), ""))


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def match_people(text):
    t = text.lower()
    return [name for name, aliases in PEOPLE.items()
            if any(a in t for a in aliases)]


def fetch_youtube():
    """用 yt-dlp 拉取频道最近视频，按标题匹配人名。失败时降级跳过，不影响整体。"""
    items = []
    cutoff = datetime.date.today() - datetime.timedelta(days=DAYS)
    for channel, url in YOUTUBE_CHANNELS:
        try:
            out = subprocess.run(
                ["yt-dlp", "--flat-playlist", "--quiet", "--no-warnings",
                 "--extractor-args", "youtube:player_client=android",
                 "--playlist-end", "15",
                 "--print", "%(id)s|%(title)s|%(upload_date)s",
                 url],
                capture_output=True, text=True, timeout=300)
            for line in out.stdout.splitlines():
                parts = line.split("|", 2)
                if len(parts) < 2 or not parts[0]:
                    continue
                vid, title = parts[0], parts[1].strip()
                upload = parts[2].strip() if len(parts) > 2 else ""
                if upload and len(upload) == 8:
                    try:
                        if datetime.datetime.strptime(upload, "%Y%m%d").date() < cutoff:
                            continue
                    except ValueError:
                        pass
                people = match_people(title)
                if not people:
                    continue
                items.append({
                    "title": title,
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "source": f"{channel} (YouTube)",
                    "date": f"{upload[:4]}-{upload[4:6]}-{upload[6:8]}" if len(upload) == 8 else "未知",
                    "people": people,
                    "snippet": "",
                })
            print(f"[ok] YouTube 频道完成: {channel}")
        except Exception as e:
            print(f"[warn] YouTube 频道抓取失败（已跳过，不影响其他信源）: {channel}: {e}",
                  file=sys.stderr)
    return items


def fetch_news():
    """Google News RSS，按人名检索。失败时降级跳过。"""
    items = []
    cutoff_ts = time.time() - DAYS * 86400
    for person, query in NEWS_QUERIES.items():
        try:
            rss = "https://news.google.com/rss/search?" + urllib.parse.urlencode({
                "q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})
            feed = feedparser.parse(rss)
            for e in feed.entries:
                ts = time.mktime(e.published_parsed) if e.get("published_parsed") else 0
                if ts and ts < cutoff_ts:
                    continue
                src = "Google News"
                if e.get("source") and getattr(e["source"], "get", None):
                    src = e["source"].get("title", src)
                items.append({
                    "title": e.get("title", "").strip(),
                    "url": e.get("link", "").strip(),
                    "source": src,
                    "date": datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "未知",
                    "people": [person],
                    "snippet": re.sub(r"<[^>]+>", "", e.get("summary", ""))[:400],
                })
            print(f"[ok] 新闻检索完成: {person}")
        except Exception as e:
            print(f"[warn] 新闻检索失败（已跳过）: {person}: {e}", file=sys.stderr)
    return items


def llm_summarize(person, items):
    """调用 OpenAI 兼容接口生成摘要。未配置 key 返回 None；调用失败返回 {}（降级为原文简介）。"""
    key = os.environ.get("LLM_API_KEY", "").strip()
    if not key:
        return None
    base = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
    payload_items = [{"标题": it["title"], "来源": it["source"],
                      "简介": it["snippet"][:300]} for it in items]
    prompt = (
        f"以下是过去一周关于 {person} 的公开动态线索（标题+简介）。"
        "请为每条输出一句话中文摘要（客观、不夸大）和一个公众号选题角度（注明适合深度长文还是短讯快评）。"
        "严格返回 JSON 对象，格式：{\"results\": [{\"标题\": <原文标题>, \"摘要\": ..., \"选题角度\": ...}]}。"
        f"共 {len(payload_items)} 条：\n" + json.dumps(payload_items, ensure_ascii=False)
    )
    try:
        r = requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model,
                  "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.3},
            timeout=120)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        data = json.loads(content)
        arr = data if isinstance(data, list) else data.get("results", [])
        return {x.get("标题", ""): x for x in arr if isinstance(x, dict)}
    except Exception as e:
        print(f"[warn] LLM 摘要失败（{person}），降级为原文简介: {e}", file=sys.stderr)
        return {}


def build_digest(by_person, llm_results, week_of):
    lines = [f"# AI 大佬动态监测周报（截至 {week_of}，过去 7 天）\n"]
    for person in PEOPLE:
        items = by_person.get(person, [])
        lines.append(f"\n## {person}\n")
        if not items:
            lines.append("本周无新动态。\n")
            continue
        for it in items:
            lines.append(f"### {it['title']}")
            lines.append(f"- 来源：{it['source']} ｜ 日期：{it['date']} ｜ [链接]({it['url']})\n")
            extra = (llm_results.get(person) or {}).get(it["title"])
            if extra:
                lines.append(f"- 摘要：{extra.get('摘要', '')}")
                lines.append(f"- 选题角度：{extra.get('选题角度', '')}\n")
            elif it.get("snippet"):
                lines.append(f"- 简介：{it['snippet'][:300]}\n")
    return "\n".join(lines)


def push(webhook, md, total):
    """企业微信机器人（qyapi 域名走 markdown 格式），其余按 Server酱 风格 title/desp 发送。"""
    try:
        if "qyapi.weixin.qq.com" in webhook:
            content = f"## AI 大佬动态周报\n> 本周新线索 {total} 条\n\n" + md[:3500]
            r = requests.post(webhook, json={"msgtype": "markdown",
                                             "markdown": {"content": content}}, timeout=30)
        else:
            r = requests.post(webhook, json={"title": f"AI 大佬动态周报：{total} 条新线索",
                                             "desp": md[:8000]}, timeout=30)
        print(f"[ok] 推送状态码: {r.status_code}")
    except Exception as e:
        print(f"[warn] 推送失败（不影响周报落盘）: {e}", file=sys.stderr)


def main():
    os.makedirs(DIGEST_DIR, exist_ok=True)
    state = load_state()

    found = fetch_youtube() + fetch_news()

    new_items = []
    for it in found:
        key = norm_url(it["url"])
        if key and key not in state:
            state[key] = {"title": it["title"], "date": str(datetime.date.today())}
            new_items.append(it)

    by_person = {p: [] for p in PEOPLE}
    for it in new_items:
        for p in it["people"]:
            by_person.setdefault(p, []).append(it)

    llm_results = {}
    if os.environ.get("LLM_API_KEY"):
        for person, items in by_person.items():
            if items:
                res = llm_summarize(person, items)
                if res is not None:
                    llm_results[person] = res

    today = datetime.date.today().isoformat()
    md = build_digest(by_person, llm_results, today)
    path = os.path.join(DIGEST_DIR, f"digest-{today}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)
    save_state(state)

    total = sum(len(v) for v in by_person.values())
    print(f"本周新线索 {total} 条（去重后），周报已写入 {path}")

    webhook = os.environ.get("PUSH_WEBHOOK", "").strip()
    if webhook:
        push(webhook, md, total)


if __name__ == "__main__":
    main()
