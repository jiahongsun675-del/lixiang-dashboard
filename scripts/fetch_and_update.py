#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
理想汽车B站内容监控 - 优化版爬虫
特性:
  - aiohttp 异步并发抓取
  - 多源采集：综合搜索 + 最新发布 + 汽车分区新列表
  - 动态请求头绕过 CDN 缓存
  - 增量存储（仅更新变化字段）
  - 自动生成 index.html 并 push 到 GitHub
"""

import asyncio
import aiohttp
import sqlite3
import json
import os
import re
import time
import random
import string
import subprocess
from datetime import datetime, timezone
from pathlib import Path

# ── 路径配置 ──
BASE_DIR   = Path(__file__).parent.parent
DB_PATH    = BASE_DIR / "data" / "history.db"
HTML_PATH  = BASE_DIR / "index.html"
DATA_DIR   = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# ── 监控关键词 ──
KEYWORDS = [
    "理想汽车", "理想i6", "理想L9", "理想MEGA",
    "理想L8", "理想L7", "理想L6", "理想i8",
    "理想智驾", "李想",
]

# ── 黑名单（标题含这些词的视频直接过滤）──
TITLE_BLACKLIST = ["停车", "剐蹭", "刮蹭", "车位", "乱停", "陷车", "停车场", "停车位"]

# ── 标题相关性：必须包含至少一个理想/李想汽车相关词 ──
TITLE_MUST_CONTAIN = [
    "理想", "李想",
]

# ── 纯竞品过滤：标题只含竞品词、不含理想时排除 ──
COMPETITOR_ONLY_KEYWORDS = [
    "小米SU7", "问界M", "鸿蒙智行", "阿维塔", "岚图", "深蓝汽车",
    "智界", "极氪", "腾势", "比亚迪汉", "比亚迪唐",
]

# ── 过滤阈值 ──
MIN_FANS       = 50_000   # 粉丝数 >= 5万
MIN_PLAY       = 100      # 最低播放量
SCORE_THRESHOLD = 40      # 上榜最低分
MAX_VIDEOS     = 15       # 推荐榜最多显示

# ── GitHub 配置（自动 push） ──
_token_file = BASE_DIR / "data" / ".github_token"
GITHUB_TOKEN = (os.environ.get("GITHUB_TOKEN") or
                (_token_file.read_text().strip() if _token_file.exists() else ""))
GITHUB_REMOTE = "https://jiahongsun675-del:{token}@github.com/jiahongsun675-del/lixiang-dashboard.git"
GIT_BRANCH = "gh-pages:main"


# ══════════════════════════════════════════════
# 1. 动态请求头（绕过 CDN 缓存）
# ══════════════════════════════════════════════

def random_device_id(n=32):
    return ''.join(random.choices(string.hexdigits.lower(), k=n))

def random_uuid():
    """生成标准 UUID 格式的 buvid"""
    h = random_device_id(32)
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"

def make_headers():
    ts = int(time.time() * 1000)
    buvid = random_uuid()
    uuid  = random_uuid()
    ua_list = [
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    ]
    return {
        "User-Agent": random.choice(ua_list),
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Cookie": (
            f"buvid3={buvid}; buvid4={uuid}; "
            f"_uuid={uuid}; bsource=search_bing; "
            f"sid=bilibili; fingerprint={random_device_id(16)};"
        ),
    }


# ══════════════════════════════════════════════
# 2. 多源异步抓取
# ══════════════════════════════════════════════

async def fetch_json(session, url, params=None, retries=2):
    for attempt in range(retries + 1):
        try:
            await asyncio.sleep(random.uniform(0.2, 0.8))
            async with session.get(url, params=params, headers=make_headers(), timeout=aiohttp.ClientTimeout(total=12)) as resp:
                if resp.status == 200:
                    return await resp.json(content_type=None)
        except Exception as e:
            if attempt == retries:
                print(f"  [ERR] {url} → {e}")
    return None


async def search_videos(session, keyword, order="totalrank"):
    """综合搜索 / 最新发布"""
    url = "https://api.bilibili.com/x/web-interface/search/type"
    params = {
        "search_type": "video",
        "keyword": keyword,
        "order": order,         # totalrank / pubdate / click
        "duration": 0,
        "page": 1,
        "page_size": 30,
        "_t": int(time.time()),  # cache-bust
    }
    data = await fetch_json(session, url, params)
    if not data or data.get("code") != 0:
        return []
    results = data.get("data", {}).get("result", []) or []
    videos = []
    for r in results:
        bvid = r.get("bvid") or ""
        if not re.match(r"^BV[a-zA-Z0-9]{10}$", bvid):
            continue
        videos.append({
            "bvid": bvid,
            "title": r.get("title", "").replace("<em class=\"keyword\">", "").replace("</em>", ""),
            "author": r.get("author", ""),
            "mid": r.get("mid", 0),
            "play": r.get("play", 0) if isinstance(r.get("play"), int) else 0,
            "danmaku": r.get("video_review", 0),
            "like": r.get("like", 0),
            "coin": 0,
            "favorite": r.get("favorites", 0),
            "reply": r.get("review", 0),
            "pubdate": r.get("pubdate", 0),
            "duration_str": r.get("duration", ""),
            "keyword_source": keyword,
        })
    return videos


async def fetch_newlist(session, rid=17, ps=30):
    """汽车分区最新发布（rid=17）"""
    url = "https://api.bilibili.com/x/web-interface/newlist"
    params = {"rid": rid, "type": 0, "pn": 1, "ps": ps, "_t": int(time.time())}
    data = await fetch_json(session, url, params)
    if not data or data.get("code") != 0:
        return []
    archives = data.get("data", {}).get("archives", []) or []
    return [{
        "bvid": a.get("bvid", ""),
        "title": a.get("title", ""),
        "author": a.get("owner", {}).get("name", ""),
        "mid": a.get("owner", {}).get("mid", 0),
        "play": a.get("stat", {}).get("view", 0),
        "danmaku": a.get("stat", {}).get("danmaku", 0),
        "like": a.get("stat", {}).get("like", 0),
        "coin": a.get("stat", {}).get("coin", 0),
        "favorite": a.get("stat", {}).get("favorite", 0),
        "reply": a.get("stat", {}).get("reply", 0),
        "pubdate": a.get("pubdate", 0),
        "duration_str": "",
        "keyword_source": "汽车分区",
    } for a in archives if re.match(r"^BV[a-zA-Z0-9]{10}$", a.get("bvid", ""))]


async def fetch_video_detail(session, bvid):
    """单视频详细数据（含更精确 stat）"""
    url = f"https://api.bilibili.com/x/web-interface/view"
    data = await fetch_json(session, url, {"bvid": bvid})
    if not data or data.get("code") != 0:
        return None
    d = data["data"]
    stat = d.get("stat", {})
    owner = d.get("owner", {})
    return {
        "bvid": bvid,
        "title": d.get("title", ""),
        "author": owner.get("name", ""),
        "mid": owner.get("mid", 0),
        "play": stat.get("view", 0),
        "danmaku": stat.get("danmaku", 0),
        "like": stat.get("like", 0),
        "coin": stat.get("coin", 0),
        "favorite": stat.get("favorite", 0),
        "reply": stat.get("reply", 0),
        "share": stat.get("share", 0),
        "pubdate": d.get("pubdate", 0),
        "duration_str": "",
    }


async def fetch_fan_count(session, mid):
    """获取 UP 主粉丝数（失败返回 -1 表示未知，不过滤）"""
    url = "https://api.bilibili.com/x/relation/stat"
    data = await fetch_json(session, url, {"vmid": mid})
    if data and data.get("code") == 0:
        f = data.get("data", {}).get("follower", 0)
        return f if f > 0 else -1
    return -1  # -1 = 未知，跳过粉丝过滤


async def collect_all_videos():
    """并发采集所有来源的视频"""
    print(f"[{datetime.now():%H:%M:%S}] 开始多源采集...")

    # 先连 DB 查粉丝缓存
    conn = init_db()

    connector = aiohttp.TCPConnector(limit=8, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:

        # 并发发起所有搜索任务
        tasks = []
        for kw in KEYWORDS:
            tasks.append(search_videos(session, kw, order="totalrank"))
            tasks.append(search_videos(session, kw, order="pubdate"))
        tasks.append(fetch_newlist(session, rid=17, ps=40))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # 合并去重
        seen = {}
        for batch in results:
            if isinstance(batch, Exception) or not batch:
                continue
            for v in batch:
                bvid = v.get("bvid", "")
                if bvid and bvid not in seen:
                    seen[bvid] = v
                elif bvid and seen[bvid].get("play", 0) < v.get("play", 0):
                    seen[bvid].update(v)

        print(f"  去重后 {len(seen)} 个视频，开始获取详细数据...")

        # 并发获取详细数据
        bvids = list(seen.keys())
        detail_tasks = [fetch_video_detail(session, b) for b in bvids]
        details = await asyncio.gather(*detail_tasks, return_exceptions=True)
        for detail in details:
            if isinstance(detail, dict) and detail.get("bvid"):
                bvid = detail["bvid"]
                seen[bvid].update(detail)

        # 获取粉丝数：先查缓存，未命中才 API 请求
        mid_set = {v.get("mid", 0) for v in seen.values() if v.get("mid")}
        uncached_mids = []
        mid_fans_map = {}
        for mid in mid_set:
            cached = get_cached_fans(conn, mid)
            if cached is not None:
                mid_fans_map[mid] = cached
            else:
                uncached_mids.append(mid)

        if uncached_mids:
            print(f"  获取 {len(uncached_mids)} 个 UP 主粉丝数（{len(mid_fans_map)} 个来自缓存）...")
            fan_tasks = [fetch_fan_count(session, mid) for mid in uncached_mids]
            fan_results = await asyncio.gather(*fan_tasks, return_exceptions=True)
            for mid, fans in zip(uncached_mids, fan_results):
                f = fans if isinstance(fans, int) else -1
                mid_fans_map[mid] = f
                if f > 0:
                    save_fan_cache(conn, mid, f)
        else:
            print(f"  粉丝数全部命中缓存（{len(mid_fans_map)} 个）")

        for v in seen.values():
            v["fans"] = mid_fans_map.get(v.get("mid", 0), -1)

    conn.close()
    raw_list = list(seen.values())
    print(f"  采集完成，共 {len(raw_list)} 个视频")
    return raw_list


# ══════════════════════════════════════════════
# 3. 过滤 + 评分
# ══════════════════════════════════════════════

def is_blacklisted(title):
    """黑名单词 / 无相关词 / 纯竞品 → 过滤"""
    if not title:
        return True
    # 黑名单
    if any(kw in title for kw in TITLE_BLACKLIST):
        return True
    # 标题必须含「理想」或「李想」
    if not any(kw in title for kw in TITLE_MUST_CONTAIN):
        return True
    # 纯竞品（不含任何 TITLE_MUST_CONTAIN 词 — 上面已拦，双重保险）
    return False


def score_video(v, prev_play=None):
    play     = v.get("play", 0)
    like     = v.get("like", 0)
    danmaku  = v.get("danmaku", 0)
    coin     = v.get("coin", 0)
    fav      = v.get("favorite", 0)
    reply    = v.get("reply", 0)
    fans     = v.get("fans", 0)
    pubdate  = v.get("pubdate", 0)

    now = time.time()
    age_h = (now - pubdate) / 3600 if pubdate else 999

    # 互动率
    interaction_rate = (like + danmaku) / play * 100 if play > 0 else 0
    play_scale = min(1.0, play / 50) if play < 50 else 1.0
    interaction_score = min(40, interaction_rate * 4) * play_scale

    # 增长趋势
    if prev_play and prev_play > 0:
        growth_pct = (play - prev_play) / prev_play * 100
    else:
        growth_pct = 0.0
    growth_score = min(30, growth_pct * 5)

    # 发布时效（越新越高分，30天内线性衰减）
    freshness_score = max(0, min(20, 20 * (1 - age_h / (30 * 24))))

    # 口碑评价
    sentiment_score = min(15, max(4.0, like * 0.15 + coin * 0.4 + fav * 0.08 + 2))

    # 素人加成
    underdog_bonus = 15.0 if fans < 100_000 else 0.0

    # 停滞降权
    is_stale = (age_h > 12 and growth_pct < 0.5 and play > 50)
    stagnation_penalty = -12.0 if is_stale else 0.0

    total = interaction_score + growth_score + freshness_score + sentiment_score + underdog_bonus + stagnation_penalty

    # 优先级
    if total >= 65:
        priority, priority_label = "high", "💎 强烈推荐"
    elif total >= 45:
        priority, priority_label = "medium", "🔥 推荐"
    elif total >= 30:
        priority, priority_label = "low", "可考虑"
    else:
        priority, priority_label = "skip", "暂不投流"

    return {
        **v,
        "interaction_rate": round(interaction_rate, 2),
        "growth_pct": round(growth_pct, 1),
        "age_h": round(age_h, 1),
        "is_stale": is_stale,
        "interaction_score": round(interaction_score, 1),
        "growth_score": round(growth_score, 1),
        "freshness_score": round(freshness_score, 1),
        "sentiment_score": round(sentiment_score, 1),
        "underdog_bonus": underdog_bonus,
        "stagnation_penalty": stagnation_penalty,
        "total_score": round(total, 1),
        "priority": priority,
        "priority_label": priority_label,
    }


# ══════════════════════════════════════════════
# 4. 增量存储
# ══════════════════════════════════════════════

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS video_snapshots (
        bvid TEXT, snapshot_time TEXT, play INTEGER, like_count INTEGER,
        coin INTEGER, favorite INTEGER, share INTEGER, reply INTEGER, danmaku INTEGER,
        PRIMARY KEY (bvid, snapshot_time)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS video_meta (
        bvid TEXT PRIMARY KEY, title TEXT, author TEXT, mid INTEGER,
        fans INTEGER, pubdate INTEGER, first_seen TEXT, last_seen TEXT,
        last_score REAL, keyword_source TEXT
    )""")
    # Fan count cache table (mid → fans, last updated)
    c.execute("""CREATE TABLE IF NOT EXISTS fan_cache (
        mid INTEGER PRIMARY KEY, fans INTEGER, updated_at TEXT
    )""")
    conn.commit()
    return conn


def get_cached_fans(conn, mid):
    """从 DB 获取缓存的粉丝数（6小时内有效）"""
    c = conn.cursor()
    c.execute("SELECT fans, updated_at FROM fan_cache WHERE mid=?", (mid,))
    row = c.fetchone()
    if row:
        try:
            dt = datetime.fromisoformat(row[1])
            if (datetime.now() - dt).total_seconds() < 6 * 3600:
                return row[0]
        except Exception:
            pass
    return None


def save_fan_cache(conn, mid, fans):
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO fan_cache (mid, fans, updated_at) VALUES (?,?,?)",
              (mid, fans, datetime.now().isoformat()))
    conn.commit()


def get_prev_play(conn, bvid):
    """获取上一次快照的播放量"""
    c = conn.cursor()
    c.execute("SELECT play FROM video_snapshots WHERE bvid=? ORDER BY snapshot_time DESC LIMIT 1 OFFSET 1", (bvid,))
    row = c.fetchone()
    return row[0] if row else None


def save_snapshot(conn, v):
    """保存快照（增量：只存和上次不同的）"""
    now_str = datetime.now().isoformat()
    c = conn.cursor()
    # 检查上次快照是否和当前完全一样
    c.execute("SELECT play,like_count,coin,favorite,reply,danmaku FROM video_snapshots WHERE bvid=? ORDER BY snapshot_time DESC LIMIT 1", (v["bvid"],))
    last = c.fetchone()
    if last and last == (v.get("play"), v.get("like"), v.get("coin"), v.get("favorite"), v.get("reply"), v.get("danmaku")):
        return False  # 无变化，不存储

    c.execute("""INSERT OR REPLACE INTO video_snapshots
        (bvid, snapshot_time, play, like_count, coin, favorite, share, reply, danmaku)
        VALUES (?,?,?,?,?,?,?,?,?)""",
        (v["bvid"], now_str, v.get("play",0), v.get("like",0), v.get("coin",0),
         v.get("favorite",0), v.get("share",0), v.get("reply",0), v.get("danmaku",0)))

    # 更新 meta
    c.execute("""INSERT INTO video_meta (bvid, title, author, mid, fans, pubdate, first_seen, last_seen, last_score, keyword_source)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(bvid) DO UPDATE SET
            title=excluded.title, fans=excluded.fans, last_seen=excluded.last_seen,
            last_score=excluded.last_score""",
        (v["bvid"], v.get("title",""), v.get("author",""), v.get("mid",0),
         v.get("fans",0), v.get("pubdate",0), now_str, now_str,
         v.get("total_score",0), v.get("keyword_source","")))

    conn.commit()
    return True


# ══════════════════════════════════════════════
# 5. HTML 生成
# ══════════════════════════════════════════════

def fmt_play(n):
    if n >= 10000:
        return f"{n/10000:.1f}万"
    return f"{n:,}"

def fmt_fans(n):
    if n >= 10000:
        return f"{n/10000:.1f}万粉"
    return f"{n}粉"

def fmt_age(pubdate):
    if not pubdate:
        return "未知"
    h = (time.time() - pubdate) / 3600
    if h < 24:
        return f"{int(h)}小时前"
    return f"{int(h/24)}天前"

def fans_color(fans):
    if fans >= 1_000_000: return "#ef4444"   # 红：百万
    if fans >= 100_000:   return "#3b6eea"   # 蓝：10万+
    if fans >= 50_000:    return "#22c55e"   # 绿：5万+
    return "#f59e0b"

def score_color(s):
    if s >= 65: return "#ef4444"
    if s >= 45: return "#f59e0b"
    if s >= 30: return "#667eea"
    return "#9ca3af"

def fresh_badge(v):
    h = v.get("age_h", 999)
    if h < 12:  return '<span class="badge badge-fresh">🆕 新内容</span>'
    if h < 72:  return '<span class="badge badge-fresh">🆕 新稿</span>'
    return ""

def hot_badge(v):
    if v.get("play", 0) >= 50000:
        return '<span class="badge" style="background:#fff7ed;color:#c2410c">🔥 高热度</span>'
    return ""

def stale_badge(v):
    if v.get("is_stale"):
        return '<span class="badge badge-stale">🥶 停滞</span>'
    return ""

def underdog_badge(v):
    if v.get("underdog_bonus", 0) > 0:
        return '<span class="badge badge-underdog">⭐ 素人 +15</span>'
    return ""

def dim_bar_html(label, score, max_score, fill_color, eval_text):
    pct = min(100, score / max_score * 100)
    return f"""
                <div class="dim-row">
                    <span class="dim-lbl">{label}</span>
                    <div class="dim-bar"><div class="dim-fill" style="width:{pct:.0f}%;background:{fill_color}"></div></div>
                    <span class="dim-val">{score:.1f}/{max_score}</span>
                    <span class="dim-tag">{eval_text}</span>
                </div>"""

def video_card_full(v, rank):
    bvid     = v["bvid"]
    title    = v.get("title", "")
    author   = v.get("author", "")
    mid      = v.get("mid", 0)
    fans     = v.get("fans", 0)
    age_str  = fmt_age(v.get("pubdate"))
    play_str = fmt_play(v.get("play", 0))
    like     = v.get("like", 0)
    coin     = v.get("coin", 0)
    fav      = v.get("favorite", 0)
    reply    = v.get("reply", 0)
    ir       = v.get("interaction_rate", 0)
    score    = v.get("total_score", 0)
    prio_lbl = v.get("priority_label", "")
    growth   = v.get("growth_pct", 0)
    growth_str = f"+{growth:.1f}%" if growth > 0 else f"{growth:.1f}%"

    rank_cls = {1: "top1", 2: "top2", 3: "top3"}.get(rank, "")
    prio_cls = {"high": "badge-high", "medium": "badge-medium", "low": "badge-low"}.get(v.get("priority"), "badge-low")
    sc = score_color(score)
    penalty_note = '<span class="stale-note">⚠️ 停滞降权 -12分</span>' if v.get("is_stale") else ""
    fr_badge = fresh_badge(v)
    ht_badge = hot_badge(v)
    st_badge = stale_badge(v)
    ud_badge = underdog_badge(v)

    # 推荐理由
    reasons = []
    if ir >= 5: reasons.append(f'💯 <strong>互动率{ir:.2f}%</strong> — 优秀(≥5%)')
    elif ir >= 3: reasons.append(f'👍 互动率{ir:.2f}% — 良好(≥3%)')
    else: reasons.append(f'📊 互动率{ir:.2f}%')
    h = v.get("age_h", 999)
    if h < 72: reasons.append(f'🆕 <strong>新鲜内容</strong> — {age_str}发布，正是投流黄金期')
    if growth >= 5: reasons.append(f'🚀 快速增长 +{growth:.1f}%')
    elif v.get("is_stale"): reasons.append(f'🥶 内容停滞，已停滞降权')
    if fans < 100_000: reasons.append('⭐ <strong>素人优质内容</strong> — 投流成本更低(+15分)')
    reasons_li = "".join(f"<li>{r}</li>" for r in reasons)

    # 评分维度
    ir_eval = "优秀" if ir >= 5 else "良好" if ir >= 3 else "一般"
    gr_eval = f"+{growth:.1f}%" if growth > 0 else "数据不足"
    fr_eval = age_str
    st_eval = "正向反馈" if like > 0 else "一般"
    dims = (dim_bar_html("互动率", v.get("interaction_score", 0), 40, "#10b981", ir_eval) +
            dim_bar_html("增长趋势", v.get("growth_score", 0), 30, "#3b82f6", gr_eval) +
            dim_bar_html("发布时效", v.get("freshness_score", 0), 20, "#f59e0b", fr_eval) +
            dim_bar_html("口碑评价", v.get("sentiment_score", 0), 15, "#8b5cf6", st_eval))

    # 预算
    if score >= 65:   budget = "¥6,000–10,000元"
    elif score >= 50: budget = "¥3,000–6,000元"
    elif score >= 40: budget = "¥2,000–4,000元"
    else:             budget = "¥1,000–2,000元"

    return f"""
    <div class="video-item" data-bvid="{bvid}" data-mid="{mid}">
        <div class="video-rank {rank_cls}">#{rank}</div>
        <div class="video-info">
            <div class="video-title">
                <a href="https://www.bilibili.com/video/{bvid}" target="_blank">{title}</a>
                <span class="badge {prio_cls}">{prio_lbl}</span>{fr_badge}{ht_badge}{st_badge}{ud_badge}
            </div>
            <div class="video-meta">UP主: <a href="https://space.bilibili.com/{mid}" target="_blank" style="color:#3b6eea;text-decoration:none">{author}</a> &nbsp;<span class="fans-tag" style="color:{fans_color(fans)}">{fmt_fans(fans)}</span> | BV: {bvid} | {age_str}</div>
            <div class="recommendation-box">
                <div class="rec-title">推荐理由 {penalty_note}</div>
                <ul class="rec-list">{reasons_li}</ul>
            </div>
            <div class="video-stats">
                <span class="s-item">▶️ {play_str}</span>
                <span class="s-item">👍 {like:,} ({ir:.2f}%)</span>
                <span class="s-item">💰 投币{coin}</span>
                <span class="s-item">⭐ 收藏{fav}</span>
                <span class="s-item">💬 {reply:,}</span>
                <span class="s-item">📊 互动率{ir:.2f}%</span>
                <span class="s-item">📈 增长{growth_str}</span>
            </div>
            <div class="dim-grid">{dims}</div>
            <div class="score-row">
                <span>综合评分</span>
                <div class="score-bar"><div class="score-fill" style="width:{min(100,score)}%;background:linear-gradient(90deg,#10b981,{sc})"></div></div>
                <span class="score-val" style="color:{sc}">{score}分</span>
            </div>
            <div class="budget-row">
                <span class="budget-lbl">建议加热预算</span>
                <span class="budget-val">{budget}</span>
                <button class="btn btn-secondary btn-sm btn-mark-heat" onclick="markHeated('{bvid}',{json.dumps(title)},'{author}',{score},'{budget}')">标记加热</button>
            </div>
        </div>
    </div>"""


def video_compact(v, rank):
    bvid   = v["bvid"]
    title  = v.get("title", "")[:50] + ("…" if len(v.get("title","")) > 50 else "")
    author = v.get("author", "")
    play   = fmt_play(v.get("play", 0))
    ir     = v.get("interaction_rate", 0)
    score  = v.get("total_score", 0)
    sc     = score_color(score)
    fr_b   = fresh_badge(v)
    st_b   = stale_badge(v)
    growth = v.get("growth_pct", 0)
    g_str  = f"+{growth:.1f}%" if growth > 0 else f"{growth:.1f}%"
    return f"""
        <div class="sustained-item">
          <div class="si-rank">#{rank}</div>
          <div class="si-info">
            <div class="si-title"><a href="https://www.bilibili.com/video/{bvid}" target="_blank">{title}</a> {fr_b}{st_b}</div>
            <div class="si-meta">{author} · ▶️{play} · 互动{ir:.1f}% · 增长{g_str}</div>
          </div>
          <div class="si-score" style="color:{sc}">{score}</div>
        </div>"""


def generate_html(scored_videos, now_str):
    by_score  = sorted([v for v in scored_videos if v["total_score"] >= SCORE_THRESHOLD], key=lambda v: v["total_score"], reverse=True)
    by_play   = sorted(scored_videos, key=lambda v: v.get("play", 0), reverse=True)
    new_vids  = sorted([v for v in scored_videos if v.get("age_h", 999) <= 72], key=lambda v: v["total_score"], reverse=True)
    hot_vids  = sorted([v for v in scored_videos if v.get("play", 0) >= 30000], key=lambda v: v.get("play", 0), reverse=True)

    total_cnt     = len(scored_videos)
    recommend_cnt = len(by_score)
    high_cnt      = sum(1 for v in scored_videos if v.get("priority") == "high")
    stale_cnt     = sum(1 for v in scored_videos if v.get("is_stale"))

    # 推荐榜视频卡片
    top_cards = "".join(video_card_full(v, i+1) for i, v in enumerate(by_score[:MAX_VIDEOS]))

    # 历史上榜（精简卡片）
    history_items = "".join(f"""
        <div class="history-item">
          <div class="history-item-info">
            <div class="history-item-title"><a href="https://www.bilibili.com/video/{v['bvid']}" target="_blank">{v.get('title','')[:60]}</a>
              {"<span class='badge badge-stale'>🥶 停滞</span>" if v.get('is_stale') else ""}
              {"<span class='badge badge-alert-hist'>历史高分</span>" if v.get('total_score',0)>=60 else ""}
            </div>
            <div class="history-item-meta">{v.get('author','')} | ▶️ {fmt_play(v.get('play',0))} | {fmt_age(v.get('pubdate'))} | 评分 {v.get('total_score',0)}</div>
          </div>
          <div class="history-item-score">
            <div class="heated-score-val">{v.get('total_score',0)}</div>
            <div class="heated-score-lbl">评分</div>
          </div>
        </div>""" for v in by_play[:20])

    # 持续热度双榜
    new_items = "".join(video_compact(v, i+1) for i, v in enumerate(new_vids[:6]))
    hot_items = "".join(video_compact(v, i+1) for i, v in enumerate(hot_vids[:6]))

    # ALL_ALERTS JSON
    alerts = []
    for v in by_score[:50]:
        alerts.append({
            "bvid": v["bvid"],
            "title": v.get("title",""),
            "author": v.get("author",""),
            "score": v.get("total_score", 0),
            "alert_time": datetime.now().isoformat(),
            "is_underdog": v.get("underdog_bonus",0) > 0,
            "recommendation": v.get("priority_label",""),
            "suggested_budget": "3000-6000元" if v.get("total_score",0) >= 60 else "2000-4000元",
        })
    alerts_json = json.dumps(alerts, ensure_ascii=False)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<meta http-equiv="refresh" content="300">
<title>理想汽车 · B站内容监测</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;background:#f4f5f7;min-height:100vh;color:#1a1a2e}}
a{{color:inherit;text-decoration:none}}a:hover{{color:#3b6eea}}
.topbar{{background:#fff;border-bottom:1px solid #e8eaed;padding:0 32px;display:flex;align-items:center;justify-content:space-between;height:56px;position:sticky;top:0;z-index:100}}
.topbar-logo{{font-size:1.05em;font-weight:700;color:#1a1a2e}}.topbar-logo span{{color:#3b6eea}}
.topbar-meta{{font-size:.8em;color:#aaa}}
.topbar-refresh{{font-size:.78em;color:#3b6eea;cursor:pointer;padding:5px 12px;border:1px solid #d0dbf7;border-radius:6px;background:#f0f4ff;transition:background .2s}}
.topbar-refresh:hover{{background:#e0eaff}}
.nav-tabs{{background:#fff;border-bottom:1px solid #e8eaed;padding:0 32px;display:flex;gap:0}}
.nav-tab{{padding:14px 22px;font-size:.92em;color:#666;cursor:pointer;border-bottom:3px solid transparent;transition:all .2s;white-space:nowrap}}
.nav-tab:hover{{color:#3b6eea}}.nav-tab.active{{color:#3b6eea;border-bottom-color:#3b6eea;font-weight:600}}
.page{{display:none;padding:28px 32px;max-width:1280px;margin:0 auto}}.page.active{{display:block}}
.stats-row{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;margin-bottom:24px}}
.sc{{background:#fff;border:1px solid #e8eaed;border-radius:10px;padding:18px 20px}}
.sc .lbl{{font-size:.78em;color:#999;margin-bottom:6px}}.sc .val{{font-size:1.9em;font-weight:700;color:#1a1a2e}}
.sc .val .unit{{font-size:.42em;color:#aaa;font-weight:400;margin-left:2px}}.sc .sub{{font-size:.75em;color:#bbb;margin-top:3px}}
.card{{background:#fff;border:1px solid #e8eaed;border-radius:10px;padding:22px;margin-bottom:18px}}
.card-title{{font-size:1.05em;font-weight:700;color:#1a1a2e;margin-bottom:4px;display:flex;align-items:center;justify-content:space-between;gap:10px}}
.card-sub{{font-size:.8em;color:#aaa;margin-bottom:18px}}
.video-list{{display:grid;gap:12px}}
.video-item{{border:1px solid #e8eaed;border-radius:8px;padding:14px 16px;display:flex;gap:14px;background:#fff;transition:border-color .2s}}
.video-item:hover{{border-color:#a5b8f5}}.video-item.heated{{opacity:.45;border-style:dashed}}
.video-rank{{font-size:1.4em;font-weight:700;min-width:32px;text-align:center;color:#c0c8d8;padding-top:2px}}
.video-rank.top1{{color:#ef4444}}.video-rank.top2{{color:#f59e0b}}.video-rank.top3{{color:#10b981}}
.video-info{{flex:1;min-width:0}}
.video-title{{font-size:.97em;font-weight:600;color:#1a1a2e;margin-bottom:4px;display:flex;flex-wrap:wrap;align-items:center;gap:6px;line-height:1.4}}
.video-title a:hover{{color:#3b6eea}}.video-meta{{color:#aaa;font-size:.8em;margin-bottom:8px}}
.fans-tag{{font-size:.78em;padding:1px 6px;border-radius:4px;background:#f4f7ff;margin-left:2px}}
.badge{{display:inline-block;padding:2px 7px;border-radius:4px;font-size:.68em;font-weight:600;white-space:nowrap}}
.badge-positive{{background:#e6f9f0;color:#0d7a4e}}.badge-high{{background:#fee2e2;color:#b91c1c}}
.badge-medium{{background:#fff3cd;color:#856404}}.badge-low{{background:#e0e7ff;color:#3730a3}}
.badge-fresh{{background:#e0eaff;color:#1d4ed8}}.badge-underdog{{background:#fef3c7;color:#92400e}}
.badge-stale{{background:#fff0f0;color:#b91c1c;border:1px solid #fecaca}}
.badge-heated{{background:#e8eaed;color:#888;font-size:.68em;padding:2px 7px;border-radius:4px}}
.badge-alert-hist{{background:#ede9fe;color:#5b21b6}}
.stale-note{{font-size:.75em;color:#dc2626;font-weight:600;margin-left:6px;padding:2px 6px;background:#fff0f0;border-radius:4px;border:1px solid #fecaca}}
.video-stats{{display:flex;flex-wrap:wrap;gap:10px;margin:6px 0}}.s-item{{font-size:.8em;color:#777}}
.dim-grid{{margin:8px 0;padding:10px 12px;background:#fafbfc;border-radius:6px;display:grid;gap:6px;border:1px solid #f0f0f0}}
.dim-row{{display:grid;grid-template-columns:72px 1fr 48px 68px;align-items:center;gap:8px}}
.dim-lbl{{font-size:.76em;color:#999}}.dim-bar{{height:4px;background:#eee;border-radius:2px;overflow:hidden}}
.dim-fill{{height:100%}}.dim-val{{font-size:.76em;font-weight:600;color:#444;text-align:right}}.dim-tag{{font-size:.7em;color:#aaa;text-align:right}}
.score-row{{display:flex;align-items:center;gap:10px;margin-top:10px;padding-top:10px;border-top:1px solid #f0f0f0;font-size:.85em;color:#666}}
.score-bar{{flex:1;height:5px;background:#eee;border-radius:3px;overflow:hidden}}
.score-fill{{height:100%}}.score-val{{font-weight:700;min-width:48px;text-align:right}}
.budget-row{{display:flex;justify-content:space-between;align-items:center;margin-top:8px;padding:7px 12px;background:#f4f7ff;border-radius:6px;gap:8px}}
.budget-lbl{{font-weight:600;color:#3b6eea;font-size:.85em}}.budget-val{{font-weight:700;color:#1a40c8}}
.recommendation-box{{background:#f8f9ff;border-left:3px solid #3b6eea;padding:9px 13px;margin:8px 0;border-radius:4px}}
.rec-title{{font-weight:600;color:#1e40af;font-size:.85em;margin-bottom:4px}}
.rec-list{{margin:0;padding-left:15px;color:#444;font-size:.82em}}.rec-list li{{margin-bottom:2px;line-height:1.6}}
.btn{{padding:6px 14px;border:none;border-radius:6px;font-size:.8em;font-weight:600;cursor:pointer;transition:all .2s;white-space:nowrap}}
.btn-primary{{background:#3b6eea;color:#fff}}.btn-secondary{{background:#f1f3f9;color:#555;border:1px solid #dde2f0}}
.btn-success{{background:#e6f9f0;color:#0d7a4e;border:1px solid #b2dfc8}}.btn-sm{{padding:4px 10px;font-size:.75em}}
.heated-list{{display:grid;gap:10px}}
.heated-item{{border:1px solid #e8eaed;border-radius:8px;padding:13px 16px;display:flex;justify-content:space-between;align-items:flex-start;gap:12px}}
.heated-item-info{{flex:1;min-width:0}}.heated-item-title{{font-size:.93em;font-weight:600;color:#1a1a2e;margin-bottom:3px}}
.heated-item-meta{{font-size:.78em;color:#aaa}}.heated-item-score{{text-align:right;flex-shrink:0}}
.heated-score-val{{font-size:1.2em;font-weight:700;color:#3b6eea}}.heated-score-lbl{{font-size:.72em;color:#aaa}}
.history-list{{display:grid;gap:10px}}
.history-item{{border:1px solid #e8eaed;border-radius:8px;padding:13px 16px;display:flex;justify-content:space-between;align-items:flex-start;gap:12px;background:#fff}}
.history-item-info{{flex:1;min-width:0}}.history-item-title{{font-size:.93em;font-weight:600;color:#1a1a2e;margin-bottom:3px;display:flex;flex-wrap:wrap;align-items:center;gap:6px}}
.history-item-title a:hover{{color:#3b6eea}}.history-item-meta{{font-size:.78em;color:#aaa}}
.bv-input-row{{display:flex;gap:10px;margin-bottom:14px}}
.bv-input{{flex:1;padding:9px 13px;border:1px solid #dde2f0;border-radius:7px;font-size:.93em;outline:none;transition:border-color .2s}}
.bv-input:focus{{border-color:#3b6eea}}.bv-btn{{padding:9px 22px;background:#3b6eea;color:#fff;border:none;border-radius:7px;font-size:.93em;font-weight:600;cursor:pointer}}
.bv-btn:hover{{background:#2554d0}}.bv-btn:disabled{{background:#aaa;cursor:not-allowed}}
.bv-card{{border:1px solid #e8eaed;border-radius:8px;padding:16px;margin-top:4px}}
.bv-stats-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:10px 0}}
.bv-stat{{background:#f8f9fc;border-radius:7px;padding:10px;text-align:center}}
.bv-stat-l{{font-size:.75em;color:#aaa;margin-bottom:3px}}.bv-stat-v{{font-size:1.2em;font-weight:700;color:#333}}
.bv-ir{{font-size:.85em;padding:8px 12px;background:#f4f7ff;border-radius:6px;color:#334155;margin-bottom:8px}}
.bv-rec{{padding:9px 14px;border-radius:6px;font-weight:600;text-align:center;margin-top:8px;font-size:.9em}}
.bv-rec.high{{background:#e6f9f0;color:#0d7a4e}}.bv-rec.medium{{background:#fff3cd;color:#856404}}.bv-rec.low{{background:#f1f5f9;color:#64748b}}
.bv-error{{padding:12px;background:#fee2e2;border-radius:7px;color:#991b1b;font-size:.88em}}
.sustained-grid{{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-top:4px}}
@media(max-width:900px){{.sustained-grid{{grid-template-columns:1fr}}}}
.sustained-panel{{background:#fff;border:1px solid #e8eaed;border-radius:10px;padding:18px}}
.sustained-panel-title{{font-size:1em;font-weight:700;margin-bottom:12px;padding-bottom:10px;border-bottom:2px solid}}
.sustained-panel.growing .sustained-panel-title{{color:#166534;border-color:#22c55e}}
.sustained-panel.steady .sustained-panel-title{{color:#1d4ed8;border-color:#3b82f6}}
.sustained-item{{display:flex;align-items:center;gap:10px;padding:9px 10px;border:1px solid #f0f0f0;border-radius:6px;margin-bottom:7px;background:#fafbfc;transition:border-color .2s}}
.sustained-item:hover{{border-color:#a5b8f5;background:#fff}}
.si-rank{{font-size:1.05em;font-weight:700;color:#667eea;min-width:22px;text-align:center;flex-shrink:0}}
.si-info{{flex:1;min-width:0}}.si-title{{font-size:.88em;font-weight:600;color:#1a1a2e;margin-bottom:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.si-title a{{color:inherit}}.si-title a:hover{{color:#3b6eea}}.si-meta{{font-size:.74em;color:#aaa}}
.si-score{{font-weight:700;font-size:.95em;min-width:36px;text-align:right;flex-shrink:0}}
.sustained-legend{{font-size:.8em;color:#aaa;margin-bottom:14px;background:#f8f9fc;padding:8px 12px;border-radius:6px;border:1px solid #f0f0f0}}
.cfg-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px}}
.cfg-item{{padding:10px 14px;background:#f8f9fc;border-radius:7px;border:1px solid #f0f0f0}}
.cfg-l{{font-size:.76em;color:#aaa;margin-bottom:3px;font-weight:600}}.cfg-v{{font-size:.88em;color:#333;word-break:break-all}}
.empty{{text-align:center;padding:28px;color:#ccc;font-size:.9em}}
.modal-overlay{{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.4);z-index:1000;align-items:center;justify-content:center}}
.modal-overlay.show{{display:flex}}.modal{{background:#fff;border-radius:12px;padding:26px;width:90%;max-width:440px;box-shadow:0 20px 60px rgba(0,0,0,.2)}}
.modal h3{{font-size:1.05em;color:#1a1a2e;margin-bottom:14px;padding-bottom:10px;border-bottom:1px solid #e8eaed}}
.modal-field{{margin-bottom:12px}}.modal-field label{{display:block;font-size:.82em;font-weight:600;color:#444;margin-bottom:4px}}
.modal-field input{{width:100%;padding:8px 12px;border:1px solid #dde2f0;border-radius:7px;font-size:.92em;outline:none}}
.modal-actions{{display:flex;gap:10px;margin-top:18px}}.modal-submit{{flex:1;padding:9px;background:#3b6eea;color:#fff;border:none;border-radius:7px;font-size:.95em;font-weight:600;cursor:pointer}}
.modal-cancel{{padding:9px 18px;background:#f1f3f9;color:#666;border:none;border-radius:7px;font-size:.95em;cursor:pointer}}
@media(max-width:720px){{.topbar{{padding:0 16px}}.nav-tabs{{padding:0 16px}}.page{{padding:18px 16px}}.dim-row{{grid-template-columns:62px 1fr 40px 56px}}}}
</style>
</head>
<body>

<div class="topbar">
  <div class="topbar-logo">理想汽车 · <span>B站内容监测</span></div>
  <div class="topbar-meta">数据更新: {now_str} &nbsp;|&nbsp; 含{total_cnt}个视频</div>
  <button class="topbar-refresh" onclick="location.reload()">刷新</button>
</div>

<div class="nav-tabs">
  <div class="nav-tab active" data-page="page-recommend" onclick="switchPage(this)">推荐榜单</div>
  <div class="nav-tab" data-page="page-history" onclick="switchPage(this)">历史上榜</div>
  <div class="nav-tab" data-page="page-sustained" onclick="switchPage(this)">持续热度</div>
  <div class="nav-tab" data-page="page-heated" onclick="switchPage(this)">历史加热</div>
  <div class="nav-tab" data-page="page-query" onclick="switchPage(this)">BV查询</div>
  <div class="nav-tab" data-page="page-config" onclick="switchPage(this)">监控配置</div>
</div>

<!-- PAGE 1: 推荐榜单 -->
<div class="page active" id="page-recommend">
  <div class="stats-row">
    <div class="sc"><div class="lbl">本次分析</div><div class="val">{total_cnt}<span class="unit">个</span></div><div class="sub">去重后候选视频</div></div>
    <div class="sc"><div class="lbl">达标推荐</div><div class="val">{recommend_cnt}<span class="unit">个</span></div><div class="sub">评分≥{SCORE_THRESHOLD}分</div></div>
    <div class="sc"><div class="lbl">高优先级</div><div class="val">{high_cnt}<span class="unit">个</span></div><div class="sub">评分≥65分</div></div>
    <div class="sc"><div class="lbl">累计加热</div><div class="val" id="stat-heated-count">—</div><div class="sub">已标记加热</div></div>
  </div>
  <div class="card">
    <div class="card-title">
      推荐内容 TOP{min(MAX_VIDEOS, recommend_cnt)}
      <span style="font-size:.78em;font-weight:400;color:#aaa">已加热内容自动置灰 · 点击「标记加热」移入历史</span>
    </div>
    <div class="card-sub">多源异步采集 · 粉丝≥5万 · 互动率×增长趋势×发布时效×口碑评价 · 🆕新内容优先 · 🔥高热度标注 · 🥶停滞降权</div>
    <div class="video-list">
{top_cards}
    </div>
  </div>
</div>

<!-- PAGE 2: 历史上榜 -->
<div class="page" id="page-history">
  <div class="card">
    <div class="card-title">历史上榜推荐加热<span style="font-size:.78em;font-weight:400;color:#aaa">按播放量排序 · TOP20</span></div>
    <div class="card-sub">曾进入推荐榜的视频 · 按累计播放量排序 · 🥶停滞标签表示增速放缓</div>
    <div class="history-list">
{history_items}
    </div>
  </div>
</div>

<!-- PAGE 3: 持续热度 -->
<div class="page" id="page-sustained">
  <div class="card">
    <div class="card-title">持续热度双榜<span style="font-size:.78em;font-weight:400;color:#aaa">新内容榜 + 高热度榜</span></div>
    <div class="card-sub">左榜：72小时内新鲜内容，投流ROI最高；右榜：播放量≥3万，品牌曝光首选</div>
    <div class="sustained-legend">💡 <b>新内容榜</b>：趁热追投，窗口期短，ROI最高 &nbsp;|&nbsp; <b>高热度榜</b>：受众基数大，持续曝光首选</div>
    <div class="sustained-grid">
      <div class="sustained-panel growing">
        <div class="sustained-panel-title">🆕 新内容榜 · 72h内新鲜内容</div>
        {new_items or '<div class="empty">暂无72小时内新发布视频</div>'}
      </div>
      <div class="sustained-panel steady">
        <div class="sustained-panel-title">🔥 高热度榜 · 播放量≥3万</div>
        {hot_items or '<div class="empty">暂无高播放量视频</div>'}
      </div>
    </div>
  </div>
</div>

<!-- PAGE 4: 历史加热 -->
<div class="page" id="page-heated">
  <div class="card">
    <div class="card-title">历史加热记录<span style="font-size:.78em;font-weight:400;color:#aaa">本地存储 · 刷新保留</span></div>
    <div class="heated-list" id="heated-list"><div class="empty">暂无本地标记的加热记录</div></div>
  </div>
</div>

<!-- PAGE 5: BV查询 -->
<div class="page" id="page-query">
  <div class="card">
    <div class="card-title">BV查询</div>
    <div class="bv-input-row">
      <input class="bv-input" id="bv-input" placeholder="输入BV号或视频链接，例如 BV1u89KBvE73" />
      <button class="bv-btn" id="bv-query-btn" onclick="queryBV()">查询</button>
    </div>
    <div id="bv-result"></div>
  </div>
</div>

<!-- PAGE 6: 监控配置 -->
<div class="page" id="page-config">
  <div class="card">
    <div class="card-title">监控配置</div>
    <div class="cfg-grid">
      <div class="cfg-item"><div class="cfg-l">采集方式</div><div class="cfg-v">aiohttp 异步并发（多源采集）</div></div>
      <div class="cfg-item"><div class="cfg-l">数据来源</div><div class="cfg-v">综合搜索 + 最新发布 + 汽车分区新列表</div></div>
      <div class="cfg-item"><div class="cfg-l">粉丝门槛</div><div class="cfg-v">≥5万粉</div></div>
      <div class="cfg-item"><div class="cfg-l">上榜最低分</div><div class="cfg-v">{SCORE_THRESHOLD}分</div></div>
      <div class="cfg-item"><div class="cfg-l">停滞降权</div><div class="cfg-v">发布>12h 且增长<0.5% → -12分</div></div>
      <div class="cfg-item"><div class="cfg-l">黑名单关键词</div><div class="cfg-v">停车、剐蹭、刮蹭、车位、乱停、陷车</div></div>
      <div class="cfg-item"><div class="cfg-l">标题相关性</div><div class="cfg-v">必须含「理想」或「李想」，排除竞品/无关视频</div></div>
      <div class="cfg-item"><div class="cfg-l">监控关键词</div><div class="cfg-v">{', '.join(KEYWORDS)}</div></div>
      <div class="cfg-item"><div class="cfg-l">增量存储</div><div class="cfg-v">仅保存变化字段，data/history.db</div></div>
      <div class="cfg-item"><div class="cfg-l">数据更新时间</div><div class="cfg-v">{now_str}</div></div>
    </div>
  </div>
</div>

<!-- 投流弹窗 -->
<div class="modal-overlay" id="promo-modal">
  <div class="modal">
    <h3>📋 记录投流信息</h3>
    <div class="modal-video-title" id="modal-video-title"></div>
    <div class="modal-field"><label>投流预算 (元)</label><input id="modal-budget" type="number" placeholder="例如 3000"></div>
    <div class="modal-field"><label>备注</label><input id="modal-note" placeholder="投流平台、时段等备注"></div>
    <div class="modal-actions">
      <button class="modal-submit" onclick="submitPromotion()">确认加热</button>
      <button class="modal-cancel" onclick="closeModal()">取消</button>
    </div>
  </div>
</div>

<script>
const TITLE_BLACKLIST = ['停车','剐蹭','刮蹭','车位','乱停','陷车','停车场','停车位'];
const TITLE_MUST_CONTAIN = ['理想','李想'];
function applyBlacklistFilter(){{
  document.querySelectorAll('.video-item').forEach(el=>{{
    const t=el.querySelector('.video-title a');
    if(!t) return;
    const txt=t.textContent;
    const blocked=TITLE_BLACKLIST.some(kw=>txt.includes(kw)) || !TITLE_MUST_CONTAIN.some(kw=>txt.includes(kw));
    if(blocked) el.style.display='none';
  }});
}}
function switchPage(tab){{
  document.querySelectorAll('.nav-tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  tab.classList.add('active');
  document.getElementById(tab.dataset.page).classList.add('active');
}}
function getHeatedSet(){{try{{return new Set(JSON.parse(localStorage.getItem('heated_bvids')||'[]'));}}catch{{return new Set();}}}}
function saveHeatedSet(s){{localStorage.setItem('heated_bvids',JSON.stringify([...s]));}}
let _pendingHeat=null;
function markHeated(bvid,title,author,score,budget){{
  const s=getHeatedSet();s.add(bvid);saveHeatedSet(s);
  const records=getLocalHeatedRecords();
  if(!records.find(r=>r.bvid===bvid)){{records.unshift({{bvid,title,author,score,budget,heated_at:new Date().toISOString()}});localStorage.setItem('heated_records',JSON.stringify(records));}}
  refreshHeatedUI();
  _pendingHeat={{bvid,title}};
  document.getElementById('modal-video-title').textContent=title;
  document.getElementById('modal-budget').value=budget.replace(/[^0-9]/g,'');
  document.getElementById('promo-modal').classList.add('show');
}}
function unmarkHeated(bvid){{
  const s=getHeatedSet();s.delete(bvid);saveHeatedSet(s);
  const records=getLocalHeatedRecords().filter(r=>r.bvid!==bvid);
  localStorage.setItem('heated_records',JSON.stringify(records));
  refreshHeatedUI();
}}
function getLocalHeatedRecords(){{try{{return JSON.parse(localStorage.getItem('heated_records')||'[]');}}catch{{return[];}}}}
function refreshHeatedUI(){{
  const s=getHeatedSet();
  document.querySelectorAll('.video-item').forEach(el=>{{
    const bvid=el.dataset.bvid;if(!bvid)return;
    if(s.has(bvid)){{
      el.classList.add('heated');
      const btn=el.querySelector('.btn-mark-heat');
      if(btn){{btn.textContent='已加热';btn.classList.remove('btn-secondary');btn.classList.add('btn-success');btn.onclick=()=>unmarkHeated(bvid);}}
    }}else{{el.classList.remove('heated');const btn=el.querySelector('.btn-mark-heat');if(btn){{btn.textContent='标记加热';btn.classList.add('btn-secondary');btn.classList.remove('btn-success');}}}}
  }});
  document.getElementById('stat-heated-count').textContent=s.size;
  renderHeatedList();
}}
function renderHeatedList(){{
  const records=getLocalHeatedRecords();const container=document.getElementById('heated-list');
  if(!records.length){{container.innerHTML='<div class="empty">暂无本地标记的加热记录</div>';return;}}
  const fmt=iso=>{{try{{return new Date(iso).toLocaleString('zh-CN',{{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}});}}catch{{return iso;}}}};
  container.innerHTML=records.map(r=>`<div class="heated-item"><div class="heated-item-info"><div class="heated-item-title"><a href="https://www.bilibili.com/video/${{r.bvid}}" target="_blank">${{r.title||r.bvid}}</a></div><div class="heated-item-meta">UP主: ${{r.author||'—'}} | 加热时间: ${{fmt(r.heated_at)}} | BV: ${{r.bvid}}</div></div><div class="heated-item-score"><div class="heated-score-val">${{r.score?r.score.toFixed(1):'—'}}</div><div class="heated-score-lbl">评分</div><button class="btn btn-secondary btn-sm" style="margin-top:6px" onclick="unmarkHeated('${{r.bvid}}')">取消</button></div></div>`).join('');
}}
function closeModal(){{document.getElementById('promo-modal').classList.remove('show');}}
function submitPromotion(){{closeModal();}}
const ALL_ALERTS={alerts_json};
function renderAlertList(){{
  const container=document.getElementById('alert-list-container');if(!container)return;
  const filtered=ALL_ALERTS.filter(a=>!TITLE_BLACKLIST.some(kw=>(a.title||'').includes(kw)) && TITLE_MUST_CONTAIN.some(kw=>(a.title||'').includes(kw)));
  if(!filtered.length){{container.innerHTML='<div class="empty">暂无提醒记录</div>';return;}}
  const fmt=iso=>{{try{{return new Date(iso).toLocaleString('zh-CN',{{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}});}}catch{{return iso;}}}};
  container.innerHTML=filtered.slice(0,30).map(a=>`<div class="heated-item"><div class="heated-item-info"><div class="heated-item-title"><a href="https://www.bilibili.com/video/${{a.bvid}}" target="_blank">${{a.title||a.bvid}}</a>${{a.is_underdog?' <span class="badge badge-underdog">⭐素人</span>':''}}</div><div class="heated-item-meta">UP主: ${{a.author||'—'}} | 建议: ${{a.suggested_budget||'—'}} | 提醒时间: ${{fmt(a.alert_time)}}</div></div><div class="heated-item-score"><div class="heated-score-val">${{a.score?a.score.toFixed(1):'—'}}</div><div class="heated-score-lbl">评分</div></div></div>`).join('');
}}
async function queryBV(){{
  const inp=document.getElementById('bv-input').value.trim();
  const m=inp.match(/BV[a-zA-Z0-9]{{10}}/);
  if(!m){{document.getElementById('bv-result').innerHTML='<div class="bv-error">请输入有效的BV号</div>';return;}}
  const bvid=m[0];
  const btn=document.getElementById('bv-query-btn');btn.disabled=true;btn.textContent='查询中...';
  document.getElementById('bv-result').innerHTML='<div class="bv-loading">正在查询...</div>';
  try{{
    const r=await fetch(`https://api.bilibili.com/x/web-interface/view?bvid=${{bvid}}`);
    const d=await r.json();
    if(d.code!==0){{document.getElementById('bv-result').innerHTML=`<div class="bv-error">${{d.message}}</div>`;return;}}
    const{{title,owner,stat}}=d.data;
    const ir=((stat.like+stat.danmaku)/stat.view*100).toFixed(2);
    const recCls=ir>=5?'high':ir>=3?'medium':'low';
    const recTxt=ir>=5?'✅ 强烈推荐投流':ir>=3?'✅ 适合投流':'⚠️ 谨慎投流';
    document.getElementById('bv-result').innerHTML=`<div class="bv-card"><div class="bv-card-title"><a href="https://www.bilibili.com/video/${{bvid}}" target="_blank">${{title}}</a></div><div style="font-size:.8em;color:#aaa;margin-bottom:8px">UP主: ${{owner.name}} | BV: ${{bvid}}</div><div class="bv-stats-grid"><div class="bv-stat"><div class="bv-stat-l">播放</div><div class="bv-stat-v">${{stat.view>=10000?(stat.view/10000).toFixed(1)+'万':stat.view}}</div></div><div class="bv-stat"><div class="bv-stat-l">点赞</div><div class="bv-stat-v">${{stat.like}}</div></div><div class="bv-stat"><div class="bv-stat-l">投币</div><div class="bv-stat-v">${{stat.coin}}</div></div><div class="bv-stat"><div class="bv-stat-l">收藏</div><div class="bv-stat-v">${{stat.favorite}}</div></div><div class="bv-stat"><div class="bv-stat-l">评论</div><div class="bv-stat-v">${{stat.reply}}</div></div><div class="bv-stat"><div class="bv-stat-l">弹幕</div><div class="bv-stat-v">${{stat.danmaku}}</div></div></div><div class="bv-ir">互动率 <strong>${{ir}}%</strong> = (点赞${{stat.like}} + 弹幕${{stat.danmaku}}) / 播放${{stat.view}}</div><div class="bv-rec ${{recCls}}">${{recTxt}}</div></div>`;
  }}catch(e){{document.getElementById('bv-result').innerHTML=`<div class="bv-error">查询失败: ${{e.message}}</div>`;}}
  btn.disabled=false;btn.textContent='查询';
}}
(function init(){{
  applyBlacklistFilter();
  refreshHeatedUI();
}})();
</script>
</body>
</html>"""
    return html


# ══════════════════════════════════════════════
# 6. Git push
# ══════════════════════════════════════════════

def git_push(token, message):
    if not token:
        print("[SKIP] 未设置 GITHUB_TOKEN，跳过推送")
        return False
    remote = GITHUB_REMOTE.format(token=token)
    cwd = str(BASE_DIR)
    try:
        subprocess.run(["git", "add", "index.html"], cwd=cwd, check=True)
        subprocess.run(["git", "commit", "-m", message,
                        "--author", "Monitor Bot <monitor@lixiang.local>"],
                       cwd=cwd, check=True, capture_output=True)
        result = subprocess.run(
            ["git", "push", remote, GIT_BRANCH],
            cwd=cwd, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"[GIT] 推送成功")
            return True
        else:
            print(f"[GIT] 推送失败: {result.stderr}")
            return False
    except subprocess.CalledProcessError as e:
        print(f"[GIT] 命令失败: {e}")
        return False


# ══════════════════════════════════════════════
# 7. 主流程
# ══════════════════════════════════════════════

async def main():
    start = time.time()
    print(f"\n{'='*60}")
    print(f"理想汽车B站监控 - 多源异步采集")
    print(f"启动时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}")

    # Step 1: 采集
    raw_videos = await collect_all_videos()

    # Step 2: 过滤（黑名单 + 播放量 + 粉丝数）
    # fans=-1 表示 API 未返回，不过滤（宁可放行也不误杀）
    filtered = [v for v in raw_videos
                if not is_blacklisted(v.get("title", ""))
                and v.get("play", 0) >= MIN_PLAY
                and (v.get("fans", -1) == -1 or v.get("fans", 0) >= MIN_FANS)]
    print(f"过滤后: {len(filtered)} 个视频 (黑名单/低播放/低粉丝已排除)")

    # Step 3: 增量存储 + 评分
    conn = init_db()
    scored = []
    changed_count = 0
    for v in filtered:
        prev_play = get_prev_play(conn, v["bvid"])
        sv = score_video(v, prev_play)
        if save_snapshot(conn, sv):
            changed_count += 1
        scored.append(sv)
    conn.close()
    print(f"数据变化: {changed_count}/{len(filtered)} 个视频有更新")

    # Step 4: 生成 HTML（仅在有足够视频时才覆盖）
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if len(scored) < 3:
        print(f"[SKIP] 视频数量不足（{len(scored)}个），保留现有 index.html 不覆盖")
        return

    html = generate_html(scored, now_str)
    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"已生成 index.html ({len(html)//1024}KB)")

    # Step 5: 推送
    token = GITHUB_TOKEN or os.environ.get("GITHUB_TOKEN", "")
    if token:
        git_push(token, f"Auto update dashboard {now_str} ({len(scored)} videos)")
    else:
        print("[INFO] 未设置 GITHUB_TOKEN，跳过自动推送")
        print("[INFO] 手动推送: cd lixiang-dashboard && git add index.html && git push")

    elapsed = time.time() - start
    print(f"\n完成！耗时 {elapsed:.1f}s，推荐榜 {sum(1 for v in scored if v['total_score']>=SCORE_THRESHOLD)} 个视频")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    asyncio.run(main())
