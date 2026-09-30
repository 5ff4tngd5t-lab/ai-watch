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
import html
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
    "Jensen Huang":     '"Jensen Huang" (interview OR keynote OR Computex OR GTC OR earnings) when:7d',
    "Mark Zuckerberg":  '"Mark Zuckerberg" (interview OR podcast OR keynote OR speech OR Llama) when:7d',
    "Satya Nadella":    '"Satya Nadella" (interview OR podcast OR keynote OR earnings) when:7d',
    "Marc Andreessen":  '"Marc Andreessen" (interview OR podcast OR essay OR a16z) when:7d',
    "Elon Musk":        '"Elon Musk" (xAI OR Grok) (interview OR podcast OR keynote OR launch) when:7d',
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

# 每人新闻线索上限（Google News 按相关度排序，取前 N 条最有价值的）
MAX_NEWS_PER_PERSON = 10

# ---------- 播客 RSS 源（2026-09 实测可用，经 iTunes API 确认的官方 feed） ----------
PODCAST_FEEDS = [
    ("Dwarkesh Podcast",    "https://apple.dwarkesh-podcast.workers.dev/feed.rss"),
    ("Lex Fridman Podcast", "https://lexfridman.com/feed/podcast/"),
    ("Acquired",            "https://feeds.transistor.fm/acquired"),
    ("All-In Podcast",      "https://rss.libsyn.com/shows/254861/destinations/1928300.xml"),
    ("The a16z Show",       "https://feeds.simplecast.com/JGE3yC0V"),
    ("20VC",                "https://rss.libsyn.com/shows/61840/destinations/240976.xml"),
]


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


def fetch_podcasts():
    """播客 RSS 源，取窗口内新剧集并按标题匹配人名。失败时降级跳过。"""
    items = []
    cutoff_ts = time.time() - DAYS * 86400
    for show, feed_url in PODCAST_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            n = 0
            for e in feed.entries:
                ts = time.mktime(e.published_parsed) if e.get("published_parsed") else 0
                if ts and ts < cutoff_ts:
                    continue
                title = html.unescape(e.get("title", "")).strip()
                people = match_people(title)
                if not people:
                    continue
                items.append({
                    "title": title,
                    "url": e.get("link", "").strip(),
                    "source": f"{show} (播客)",
                    "date": datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "未知",
                    "people": people,
                    "snippet": "",
                })
                n += 1
            print(f"[ok] 播客源完成: {show} ({n} 条命中)")
        except Exception as e:
            print(f"[warn] 播客源抓取失败（已跳过）: {show}: {e}", file=sys.stderr)
    return items


def fetch_news():
    """Google News RSS，按人名检索（每人最多保留 MAX_NEWS_PER_PERSON 条）。失败时降级跳过。"""
    items = []
    cutoff_ts = time.time() - DAYS * 86400
    for person, query in NEWS_QUERIES.items():
        try:
            rss = "https://news.google.com/rss/search?" + urllib.parse.urlencode({
                "q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})
            feed = feedparser.parse(rss)
            n = 0
            for e in feed.entries:
                if n >= MAX_NEWS_PER_PERSON:
                    break
                ts = time.mktime(e.published_parsed) if e.get("published_parsed") else 0
                if ts and ts < cutoff_ts:
                    continue
                src = "Google News"
                if e.get("source") and getattr(e["source"], "get", None):
                    src = e["source"].get("title", src)
                items.append({
                    "title": html.unescape(e.get("title", "")).strip(),
                    "url": e.get("link", "").strip(),
                    "source": src,
                    "date": datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "未知",
                    "people": [person],
                    "snippet": html.unescape(re.sub(r"<[^>]+>", "", e.get("summary", "")))[:400],
                })
                n += 1
            print(f"[ok] 新闻检索完成: {person} ({n} 条)")
        except Exception as e:
            print(f"[warn] 新闻检索失败（已跳过）: {person}: {e}", file=sys.stderr)
    return items


def llm_weekly_report(by_person, today):
    """调用 OpenAI 兼容接口生成整周选题周报（WorkBuddy 版式）。

    返回 Markdown 字符串；未配置 key 返回 None；调用失败返回 ""（外层降级为简版）。
    """
    key = os.environ.get("LLM_API_KEY", "").strip()
    if not key:
        return None
    base = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("LLM_MODEL", "gpt-4o-mini")

    payload = {p: [{"标题": it["title"], "来源": it["source"], "日期": it["date"],
                    "简介": it["snippet"], "链接": it["url"]} for it in items]
               for p, items in by_person.items() if items}

    prompt = f"""你是资深科技公众号「AI 大佬监测」周刊的主笔。以下是本周（截至 {today}，过去 7 天）对 11 位 AI 行业领袖的公开动态监测数据，按人物分组，每条含标题、来源、日期、简介、链接：

【数据】
{json.dumps(payload, ensure_ascii=False, indent=1)}

【任务】基于以上材料撰写本周选题周报，严格使用 Markdown，结构如下（章节标题照抄）：

# AI 大佬动态监测 · 选题周报

开头一行加粗：「{today} · 监测窗口：过去 7 天」，随后用一个表格概述本期（监测名单 / 信源构成 / 本周基调一句话）。

## 本周最重磅
150–250 字叙事，提炼本周最有张力的一条主线（通常是人物观点冲突或标志性事件）。

## 一、本周 10 条核心动态速览
Markdown 表格：| # | 人物 | 标题 | 关键数据点 | 来源 |。从数据中选出最有价值的 10 条；「关键数据点」列必须简短，只使用数据中出现的事实。表格下方加一段「另含速览」：用一句话串讲其余条目（用 · 分隔）。

## 二、本周 5 条候选选题
5 张选题卡，每张严格使用如下结构：
### #N 选题类型 · 主题
**主标题：** …
- **方向标签**：…
- **爆款公式**：…
- **预估阅读量**：★ 评级（1–5 星）
- **一句话钩子**：…
**备选标题**（3–4 个，每个末尾用〔〕标注风格，如〔悬念〕〔争议〕〔反常识〕）
**关键数据点**（编号列表，只使用数据中的事实）
**切入角度建议**（一段，给出真正可写的题眼，不要泛泛而谈）
**律师视角（自然带出）**（一段，从法律/合规角度补一层观察）
**参考来源**（从数据中选 3–4 条：来源｜标题 — 链接）
**适配受众**（一行）

选题优先级：人物言论拆解、产业叙事冲突、有争议或反差的事件；兼顾「深度长文」与「短讯快评」两种形态，5 张卡里至少 1 张短讯快评型。

## 三、本周覆盖自查
表格：| 人物 | 动态条数 | 核心事件 | 入选选题 |。11 人全部列出，本周无动态的人物在核心事件列标注「观察项」。

## 四、本周叙事总结
2–3 条主线，每条一段；最后加一行「下周看点」。

## 五、下周线索池
3–4 条编号线索，每条基于本周事件做合理展望，不确定处用「盯：」引出跟踪点；最后加一行「附加跟踪」。

## 六、监控关键词库
按 3–4 个主题分组列出本期关键词（人物与言论 / 公司与产品 / 治理与监管 / 赛道与技术）。

【硬性规则】
1. 只能使用【数据】中出现的事实、数字与引语，禁止编造；材料没说的字段写「材料未提及」，不要虚构。
2. 简介为空的条目（YouTube/播客）允许基于标题做保守概括，但不得添加具体数字与引语。
3. 全文中文；人物首次出现给出中英对照。
4. 直接输出 Markdown 正文，不要输出任何解释或寒暄。"""

    try:
        r = requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model,
                  "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.4,
                  "max_tokens": 32000},
            timeout=900)
        r.raise_for_status()
        resp = r.json()
        choice = resp.get("choices", [{}])[0]
        msg = choice.get("message", {}) or {}
        content = (msg.get("content") or "").strip()
        if not content:
            print(f"[warn] LLM 返回空内容 (finish_reason={choice.get('finish_reason')}): "
                  f"{str(resp)[:300]}", file=sys.stderr)
            return ""
        # 去掉模型可能包裹的 ```markdown 代码围栏
        content = re.sub(r"^```(?:markdown)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
        return content if content.startswith("#") else "# " + content
    except Exception as e:
        print(f"[warn] LLM 周报生成失败，降级为简版周报: {e}", file=sys.stderr)
        return ""


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


def norm_title(t):
    """标题归一化，用于同一轮内跨信源去重（同一期节目可能同时出现在 YouTube 和 RSS）。"""
    return re.sub(r"[\W_]+", " ", t.lower()).strip()


def main():
    os.makedirs(DIGEST_DIR, exist_ok=True)
    state = load_state()

    found = fetch_youtube() + fetch_podcasts() + fetch_news()

    new_items = []
    seen_titles = set()
    for it in found:
        key = norm_url(it["url"])
        if not key or key in state:
            continue
        tkey = norm_title(it["title"])
        if tkey and tkey in seen_titles:
            continue
        seen_titles.add(tkey)
        state[key] = {"title": it["title"], "date": str(datetime.date.today())}
        new_items.append(it)

    by_person = {p: [] for p in PEOPLE}
    for it in new_items:
        for p in it["people"]:
            by_person.setdefault(p, []).append(it)

    today = datetime.date.today().isoformat()

    # 优先用 LLM 生成 WorkBuddy 版式整周报告；未配置 key 或调用失败时降级为简版
    report = llm_weekly_report(by_person, today)
    is_full = bool(report)
    if not is_full:
        report = build_digest(by_person, {}, today)
    path = os.path.join(DIGEST_DIR,
                        f"report-{today}.md" if is_full else f"digest-{today}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(report)
    save_state(state)

    total = sum(len(v) for v in by_person.values())
    print(f"本周新线索 {total} 条（去重后），周报已写入 {path}"
          f"（{'LLM 完整版' if is_full else '简版'}）")

    webhook = os.environ.get("PUSH_WEBHOOK", "").strip()
    if webhook:
        push(webhook, report, total)


if __name__ == "__main__":
    main()
