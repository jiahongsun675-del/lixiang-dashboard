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
TITLE_BLACKLIST = [
    # 停车/剐蹭类
    "停车", "剐蹭", "刮蹭", "车位", "乱停", "陷车", "停车场", "停车位",
    # 火车/进行曲类（无关内容）
    "火车", "理想进行曲", "铁路", "高铁", "动车",
    # 求职/招聘类（理想汽车公司相关，非汽车本身）
    "offer", "求职", "招聘", "应届", "算法岗", "工作邀约", "大厂", "字节",
    "华为招", "实习生", "校招", "社招",
]

# ── 标题相关性：必须包含汽车品牌/车型/车主等明确车辆上下文 ──
TITLE_MUST_CONTAIN = [
    # 车型（精确）
    "理想L6", "理想L7", "理想L8", "理想L9",
    "理想i6", "理想i8", "理想MEGA", "理想ONE",
    # 品牌/车主/功能
    "理想汽车", "理想车", "理想车主", "理想智驾", "理想AD",
    "理想OTA", "理想v",    # OTA版本如 v13.4
    "理想音响", "理想座舱", "理想增程",
    # 兜底：标题中出现「理想」（用于食贫道等宽泛标题，配合黑名单+竞品过滤）
    "理想",
    # CEO 相关
    "李想",
]

# ── 竞品品牌词（用于识别竞品主内容视频）──
COMPETITOR_BRANDS = [
    "极氪", "智己", "问界", "蔚来", "小鹏", "阿维塔", "腾势", "岚图",
    "仰望", "深蓝", "银河", "零跑", "哪吒", "飞凡", "享界", "尊界",
    "小米YU7", "小米SU7", "小米汽车", "智界S7", "智界R7",
    "华为问界", "鸿蒙智行", "奥迪Q6", "宝马X5", "奔驰GLE",
]

# ── 对比词（有这些词时允许提及竞品）──
COMPARE_WORDS = ["vs", "VS", "对比", "PK", "pk", "哪个好", "选哪个", "怎么选",
                 "谁更", "谁强", "谁好", "谁胜", "谁划算", "还是", "区别"]

# ── 高频理想内容 UP 主 MID（自动扫新视频）──
TOP_CREATOR_MIDS = [
    2054075556,      # 理想汽车（官方）
    3546724748495813, # 超哥超车
    60259602,        # 百车全说
    39736779,        # 38号车评中心
    346802311,       # 老高你好二手车
    1339268,         # 农民新八
    3546814473046548, # 智能车研究所
    401117766,       # 德皮剪辑Sir
    283310205,       # 韩路有点意思
]


MIN_FANS       = 50_000   # 粉丝数 >= 5万
MIN_PLAY       = 100      # 最低播放量
SCORE_THRESHOLD    = 30      # 上榜最低分
MAX_VIDEOS         = 20       # 推荐榜最多显示
SEARCH_ROUNDS      = 5        # 搜索轮数（每轮3页，共覆盖15页）
HIGH_VALUE_THRESHOLD = 65     # 高价值内容实时推送阈值

# ── GitHub 配置（自动 push） ──
_token_file = BASE_DIR / "data" / ".github_token"
GITHUB_TOKEN = (os.environ.get("GITHUB_TOKEN") or
                (_token_file.read_text().strip() if _token_file.exists() else ""))
GITHUB_REMOTE = "https://jiahongsun675-del:{token}@github.com/jiahongsun675-del/lixiang-dashboard.git"
GIT_BRANCH = "gh-pages:main"

# ── 飞书 Webhook ──
FEISHU_WEBHOOK    = "https://open.feishu.cn/open-apis/bot/v2/hook/3736aa75-07a1-4580-8d84-77675d7dc149"
DASHBOARD_URL     = "https://jiahongsun675-del.github.io/lixiang-dashboard/"
PUSHED_ALERTS_FILE = BASE_DIR / "data" / "pushed_alerts.json"  # 已推送记录


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


async def search_videos(session, keyword, order="totalrank", page=1):
    """综合搜索 — 优先 search/type，降级 search/all/v2，支持多页"""
    url = "https://api.bilibili.com/x/web-interface/search/type"
    params = {
        "search_type": "video",
        "keyword": keyword,
        "order": order,
        "duration": 0,
        "page": page,
        "page_size": 30,
        "_t": int(time.time()),
    }
    data = await fetch_json(session, url, params)

    # 降级：search/all/v2（不受 412 限制，但只有第1页）
    if (not data or data.get("code") != 0) and page == 1:
        url2 = "https://api.bilibili.com/x/web-interface/search/all/v2"
        p2 = {"keyword": keyword, "search_type": "video", "_t": int(time.time())}
        data2 = await fetch_json(session, url2, p2)
        if data2 and data2.get("code") == 0:
            for section in data2.get("data", {}).get("result", []):
                if section.get("result_type") == "video":
                    data = {"code": 0, "data": {"result": section.get("data", [])}}
                    break

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


async def fetch_up_videos(session, mid, ps=20):
    """抓取指定 UP 主最新上传的视频"""
    url = "https://api.bilibili.com/x/space/wbi/arc/search"
    # 尝试不带 wbi 的旧接口
    url2 = "https://api.bilibili.com/x/space/arc/search"
    params = {"mid": mid, "ps": ps, "pn": 1, "order": "pubdate", "_t": int(time.time())}
    data = await fetch_json(session, url2, params)
    if not data or data.get("code") != 0:
        data = await fetch_json(session, url, params)
    if not data or data.get("code") != 0:
        return []
    vlist = data.get("data", {}).get("list", {}).get("vlist", []) or []
    return [{
        "bvid": v.get("bvid", ""),
        "title": v.get("title", ""),
        "author": v.get("author", ""),
        "mid": mid,
        "play": v.get("play", 0),
        "danmaku": v.get("video_review", 0),
        "like": 0,
        "coin": 0,
        "favorite": v.get("favorites", 0),
        "reply": v.get("comment", 0),
        "pubdate": v.get("created", 0),
        "duration_str": v.get("length", ""),
        "keyword_source": f"UP主:{v.get('author','')}",
    } for v in vlist if re.match(r"^BV[a-zA-Z0-9]{10}$", v.get("bvid", ""))]


async def fetch_ranking(session, rid=17, day=3):
    """B站分区热门排行（汽车 rid=17，day=3 表示3天内）"""
    url = "https://api.bilibili.com/x/web-interface/ranking/v2"
    params = {"rid": rid, "type": 0, "_t": int(time.time())}
    data = await fetch_json(session, url, params)
    if not data or data.get("code") != 0:
        return []
    items = data.get("data", {}).get("list", []) or []
    return [{
        "bvid": v.get("bvid", ""),
        "title": v.get("title", ""),
        "author": v.get("owner", {}).get("name", ""),
        "mid": v.get("owner", {}).get("mid", 0),
        "play": v.get("stat", {}).get("view", 0),
        "danmaku": v.get("stat", {}).get("danmaku", 0),
        "like": v.get("stat", {}).get("like", 0),
        "coin": v.get("stat", {}).get("coin", 0),
        "favorite": v.get("stat", {}).get("favorite", 0),
        "reply": v.get("stat", {}).get("reply", 0),
        "pubdate": v.get("pubdate", 0),
        "duration_str": "",
        "keyword_source": "汽车排行榜",
    } for v in items if re.match(r"^BV[a-zA-Z0-9]{10}$", v.get("bvid", ""))]


async def fetch_newlist(session, rid=17, ps=50, pn=1):
    """汽车分区最新发布（rid=17），支持翻页"""
    url = "https://api.bilibili.com/x/web-interface/newlist"
    params = {"rid": rid, "type": 0, "pn": pn, "ps": ps, "_t": int(time.time())}
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
    """
    获取 UP 主粉丝数（多接口备用，失败返回 0）
    优先 space/acc/info（更稳定），备用 relation/stat
    """
    endpoints = [
        ("https://api.bilibili.com/x/space/acc/info", {"mid": mid}),
        ("https://api.bilibili.com/x/relation/stat",  {"vmid": mid}),
    ]
    for url, params in endpoints:
        try:
            await asyncio.sleep(random.uniform(0.05, 0.2))
            async with session.get(url, params=params, headers=make_headers(),
                                   timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    d = await resp.json(content_type=None)
                    if d and d.get("code") == 0:
                        data = d.get("data", {})
                        # space/acc/info 返回 follower 字段
                        fans = data.get("follower") or data.get("fans") or 0
                        if fans > 0:
                            return fans
        except Exception:
            pass
    return 0  # 获取失败


async def collect_all_videos():
    """并发采集所有来源的视频（多轮）"""
    print(f"[{datetime.now():%H:%M:%S}] 开始多源采集...")

    conn = init_db()
    seen = {}

    async def run_round(round_num: int):
        connector = aiohttp.TCPConnector(limit=20, ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            tasks = []
            # 3种排序 × 3页/轮（每轮覆盖不同页码段）
            orders = ["totalrank", "pubdate", "click"]
            start_page = (round_num - 1) * 3 + 1
            pages = [start_page, start_page + 1, start_page + 2]
            for kw in KEYWORDS:
                for order in orders:
                    for pg in pages:
                        tasks.append(search_videos(session, kw, order=order, page=pg))
            # 汽车分区新列表（仅前3轮）
            if round_num <= 3:
                for pn in [round_num * 2 - 1, round_num * 2]:
                    tasks.append(fetch_newlist(session, rid=17, ps=50, pn=pn))
            # 排行榜+UP主（仅第1轮）
            if round_num == 1:
                tasks.append(fetch_ranking(session, rid=17))
                for mid in TOP_CREATOR_MIDS:
                    tasks.append(fetch_up_videos(session, mid, ps=20))

            results = await asyncio.gather(*tasks, return_exceptions=True)

            batch_new = 0
            for batch in results:
                if isinstance(batch, Exception) or not batch:
                    continue
                for v in batch:
                    bvid = v.get("bvid", "")
                    if not bvid:
                        continue
                    if bvid not in seen:
                        seen[bvid] = v
                        batch_new += 1
                    elif seen[bvid].get("play", 0) < v.get("play", 0):
                        seen[bvid].update(v)
            return batch_new

    for rnd in range(1, SEARCH_ROUNDS + 1):
        n = await run_round(rnd)
        print(f"  第{rnd}轮: +{n} 新视频，累计 {len(seen)} 个")
        if rnd < SEARCH_ROUNDS:
            await asyncio.sleep(1.5)  # 轮间小延迟防限速

    # 获取详细数据（并发）
    connector = aiohttp.TCPConnector(limit=20, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        bvids = list(seen.keys())
        details = await asyncio.gather(
            *[fetch_video_detail(session, b) for b in bvids],
            return_exceptions=True
        )
        for d in details:
            if isinstance(d, dict) and d.get("bvid"):
                seen[d["bvid"]].update(d)

    # 先做基础过滤（不含粉丝数），缩小需要查粉丝的候选集
    basic_filtered = [v for v in seen.values()
                      if not is_blacklisted(v.get("title", ""))
                      and v.get("play", 0) >= MIN_PLAY]
    print(f"  基础过滤后 {len(basic_filtered)} 个候选，开始查粉丝数...")

    # 只对候选视频查粉丝数（大幅减少 API 调用量）
    candidate_mids = list({v.get("mid", 0) for v in basic_filtered if v.get("mid")})
    mid_fans_map = {}
    uncached = []
    for mid in candidate_mids:
        # 已知大号直接跳过
        if mid in set(TOP_CREATOR_MIDS):
            mid_fans_map[mid] = 999_999
            continue
        c = get_cached_fans(conn, mid)
        if c is not None and c > 0:
            mid_fans_map[mid] = c
        else:
            uncached.append(mid)

    if uncached:
        print(f"  查粉丝数: {len(uncached)} 个新 UP 主（{len(mid_fans_map)} 缓存命中）")
        # 并发10，批间0.2s
        fan_connector = aiohttp.TCPConnector(limit=10, ssl=False)
        async with aiohttp.ClientSession(connector=fan_connector) as fan_session:
            results = []
            for i in range(0, len(uncached), 10):
                batch = uncached[i:i+10]
                br = await asyncio.gather(
                    *[fetch_fan_count(fan_session, m) for m in batch],
                    return_exceptions=True
                )
                results.extend(br)
                if i + 10 < len(uncached):
                    await asyncio.sleep(0.2)
        for mid, f in zip(uncached, results):
            fans = f if isinstance(f, int) and f > 0 else 0
            mid_fans_map[mid] = fans
            if fans > 0:
                save_fan_cache(conn, mid, fans)
    else:
        print(f"  粉丝数全部命中缓存（{len(mid_fans_map)} 个）")

    # 赋予粉丝数
    for v in basic_filtered:
        mid = v.get("mid", 0)
        v["fans"] = mid_fans_map.get(mid, 0)

    conn.close()
    raw_list = list(seen.values())
    print(f"  采集完成，共 {len(raw_list)} 个视频")
    return raw_list


# ══════════════════════════════════════════════
# 3. 过滤 + 评分
# ══════════════════════════════════════════════

def is_competitor_focused(title):
    """
    判断是否以竞品为主要内容。规则：
    1. 标题以竞品名开头（竞品是主语）
    2. 竞品名出现次数 > 理想/李想出现次数，且无对比词
    3. 竞品在标题最前且与理想相距>10字（竞品先出场，理想只是配角）
    """
    # 竞品在标题中出现的所有 brand 及其位置
    comp_hits = [(b, title.find(b)) for b in COMPETITOR_BRANDS if b in title]
    if not comp_hits:
        return False  # 没有竞品词，不过滤

    lixiang_pos = min(
        (title.find(kw) for kw in TITLE_MUST_CONTAIN if kw in title),
        default=9999
    )

    # 规则1：标题开头就是竞品（前5字内出现竞品且在理想之前）
    earliest_comp_pos = min(pos for _, pos in comp_hits)
    if earliest_comp_pos < 5 and earliest_comp_pos < lixiang_pos:
        return True

    # 规则2：竞品词数量 > 理想词数量，且无对比词
    comp_count = sum(title.count(b) for b in COMPETITOR_BRANDS)
    lixiang_count = sum(title.count(kw) for kw in TITLE_MUST_CONTAIN)
    has_compare = any(kw in title for kw in COMPARE_WORDS)
    if comp_count > lixiang_count and not has_compare:
        return True

    # 规则3：竞品排在理想前面超过8个字，且有竞品专属词（型号/测评/体验）
    COMP_EXCLUSIVE = ["实测", "评测", "体验", "试驾", "上市", "发布", "详解",
                      "怎么样", "好不好", "值得买", "深度", "全面"]
    if (earliest_comp_pos < lixiang_pos - 8
            and any(w in title for w in COMP_EXCLUSIVE)
            and not has_compare):
        return True

    return False


def is_blacklisted(title):
    """黑名单词 / 无相关词 / 竞品主内容 → 过滤"""
    if not title:
        return True
    if any(kw in title for kw in TITLE_BLACKLIST):
        return True
    if not any(kw in title for kw in TITLE_MUST_CONTAIN):
        return True
    if is_competitor_focused(title):
        return True
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

    # 新鲜度排名分（推荐分 = 互动率 × (1+增长) / (发布天数+2)^1.5）
    # 让新视频自动顶上去，老视频自然沉底
    age_days = age_h / 24
    freshness_rank = round(
        interaction_rate * (1 + growth_pct / 10) / ((age_days + 2) ** 1.5), 3
    )

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
        "freshness_rank": freshness_rank,
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
    """从 DB 获取缓存的粉丝数（24小时内有效）"""
    c = conn.cursor()
    c.execute("SELECT fans, updated_at FROM fan_cache WHERE mid=?", (mid,))
    row = c.fetchone()
    if row:
        try:
            dt = datetime.fromisoformat(row[1])
            if (datetime.now() - dt).total_seconds() < 24 * 3600:
                return row[0]  # 返回缓存值（0也返回，代表之前获取失败）
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
    <div class="video-item" data-bvid="{bvid}" data-mid="{mid}" data-age-h="{v.get('age_h',999)}">
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
    # ── 过滤：只展示1个月内的内容 ──
    monthly = [v for v in scored_videos if v.get("age_h", 999) <= 720]  # ≤30天

    # ── 分组 ──
    # 新榜单：7天内，按新鲜度排名
    fresh = sorted(
        [v for v in monthly if v.get("age_h", 999) <= 168 and v["total_score"] >= SCORE_THRESHOLD],
        key=lambda v: v.get("freshness_rank", 0), reverse=True
    )
    # 历史榜单：7-30天，按综合评分
    history_rec = sorted(
        [v for v in monthly if v.get("age_h", 999) > 168 and v["total_score"] >= SCORE_THRESHOLD],
        key=lambda v: v["total_score"], reverse=True
    )
    # 全部综合（用于其他 tab）
    by_score  = sorted([v for v in monthly if v["total_score"] >= SCORE_THRESHOLD], key=lambda v: v["total_score"], reverse=True)
    by_play   = sorted(monthly, key=lambda v: v.get("play", 0), reverse=True)
    new_vids  = sorted([v for v in monthly if v.get("age_h", 999) <= 168], key=lambda v: v.get("freshness_rank",0), reverse=True)
    hot_vids  = sorted([v for v in monthly if v.get("play", 0) >= 30000], key=lambda v: v.get("play", 0), reverse=True)

    total_cnt     = len(scored_videos)
    recommend_cnt = len(by_score)
    fresh_cnt     = len(fresh)
    high_cnt      = sum(1 for v in scored_videos if v.get("priority") == "high")
    stale_cnt     = sum(1 for v in scored_videos if v.get("is_stale"))

    # ── 推荐榜视频卡片 ──
    # 新发现组（freshness_rank排序，最多10条）
    fresh_cards = "".join(video_card_full(v, i+1) for i, v in enumerate(fresh[:10]))
    # 历史监控组（total_score排序，最多10条）
    history_cards = "".join(video_card_full(v, i+1) for i, v in enumerate(history_rec[:10]))

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
.nav-tabs{{background:#fff;border-bottom:1px solid #e8eaed;padding:0 32px;display:flex;gap:0;position:sticky;top:56px;z-index:99}}
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
/* ── 投放追踪 ── */
.promo-add-section{{border:2px dashed #dde2f0;border-radius:10px;padding:20px;margin-bottom:20px;background:#fafbff}}
.promo-add-title{{font-size:.92em;font-weight:600;color:#555;margin-bottom:14px}}
.promo-add-tabs{{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap}}
.add-tab{{padding:6px 16px;border:1px solid #dde2f0;border-radius:20px;font-size:.82em;cursor:pointer;color:#666;background:#fff;transition:all .2s}}
.add-tab.active{{background:#3b6eea;color:#fff;border-color:#3b6eea}}
.add-panel{{display:none}}.add-panel.active{{display:block}}
.add-form-row{{display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap;margin-bottom:10px}}
.add-form-field{{display:flex;flex-direction:column;gap:4px;flex:1;min-width:140px}}
.add-form-field label{{font-size:.78em;font-weight:600;color:#555}}
.add-form-field input{{padding:8px 12px;border:1px solid #dde2f0;border-radius:7px;font-size:.9em;outline:none;transition:border-color .2s}}
.add-form-field input:focus{{border-color:#3b6eea}}
.promo-list{{display:grid;gap:16px}}
.promo-card-v2{{border:1px solid #e8eaed;border-radius:10px;padding:18px;background:#fff}}
.promo-card-header{{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:14px}}
.promo-card-title{{font-size:.97em;font-weight:700;color:#1a1a2e;margin-bottom:4px;display:flex;flex-wrap:wrap;align-items:center;gap:8px}}
.promo-card-title a:hover{{color:#3b6eea}}.promo-card-meta{{font-size:.78em;color:#aaa}}
.promo-card-budget{{text-align:right;flex-shrink:0}}
.pcb-lbl{{font-size:.72em;color:#aaa;margin-bottom:2px}}.pcb-val{{font-size:1.4em;font-weight:700;color:#3b6eea}}
.promo-track-bar{{margin-bottom:14px}}
.ptb-label{{font-size:.76em;color:#aaa;margin-bottom:4px}}
.ptb-bar{{height:5px;background:#eee;border-radius:3px;overflow:hidden}}
.ptb-fill{{height:100%;background:linear-gradient(90deg,#3b6eea,#10b981);border-radius:3px}}
.promo-compare-table{{width:100%;border-collapse:collapse;font-size:.83em;margin-bottom:12px}}
.promo-compare-table th{{background:#f8f9fc;padding:7px 10px;text-align:left;color:#666;font-weight:600;border-bottom:1px solid #e8eaed}}
.promo-compare-table td{{padding:7px 10px;border-bottom:1px solid #f5f5f5;color:#333}}
.promo-compare-table tr:last-child td{{border-bottom:none}}
.promo-compare-table .delta{{color:#059669;font-weight:600}}
.promo-compare-table .pct{{color:#3b6eea;font-size:.82em}}
.promo-metrics-row{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px;margin-top:6px}}
.promo-metric{{background:#f8f9fc;border-radius:8px;padding:10px 12px;text-align:center;border:1px solid #eee}}
.pm-lbl{{font-size:.72em;color:#aaa;margin-bottom:4px}}.pm-val{{font-size:1em;font-weight:700;color:#1a1a2e}}
.promo-waiting{{padding:14px;background:#fffbeb;border:1px dashed #fbbf24;border-radius:8px;color:#92400e;font-size:.85em;text-align:center}}
.promo-roi-row{{display:flex;align-items:center;gap:8px;margin-top:10px;padding:8px 12px;background:#f4f7ff;border-radius:6px;font-size:.85em}}
.roi-lbl{{color:#666}}.roi-val{{font-weight:700;color:#3b6eea;font-size:1.05em}}
/* ── 分组标题 & 筛选按钮 ── */
.group-header{{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:.92em;font-weight:700;color:#166534;border-left:4px solid #22c55e;padding:8px 12px;background:#f0fdf4;border-radius:0 6px 6px 0;margin-bottom:12px}}
.group-count{{background:#dcfce7;color:#166534;font-size:.72em;padding:2px 8px;border-radius:10px;font-weight:600}}
.filter-btn{{padding:5px 14px;border:1px solid #dde2f0;border-radius:20px;font-size:.8em;cursor:pointer;color:#666;background:#fff;transition:all .2s}}
.filter-btn:hover{{border-color:#3b6eea;color:#3b6eea}}
.filter-btn.active{{background:#3b6eea;color:#fff;border-color:#3b6eea}}
.video-item[data-age-h]{{transition:opacity .2s}}
.video-item.time-hidden{{display:none!important}}
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
  <div class="nav-tab" data-page="page-promo" onclick="switchPage(this)">投放效果</div>
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
      推荐内容
      <span style="font-size:.78em;font-weight:400;color:#aaa">已加热自动置灰 · 点击「标记加热」移入历史</span>
    </div>
    <div class="card-sub">粉丝≥5万严格过滤 · 🆕新榜单按新鲜度排序（7天内） · 📚历史榜单按综合评分（7-30天） · 🥶停滞降权</div>

    <!-- 时效筛选按钮 -->
    <div style="display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap">
      <button class="filter-btn active" onclick="setTimeFilter(this,'all')">不限时间（1月内）</button>
      <button class="filter-btn" onclick="setTimeFilter(this,'7d')">7天内</button>
      <button class="filter-btn" onclick="setTimeFilter(this,'3d')">3天内</button>
      <span style="font-size:.78em;color:#aaa;align-self:center;margin-left:4px">按发布时间筛选</span>
    </div>

    <!-- 🆕 新榜单 -->
    <div class="group-header" id="group-fresh-header">
      🆕 新榜单 · 7天内
      <span class="group-count">{fresh_cnt} 条</span>
      <span style="font-size:.74em;font-weight:400;color:#aaa">按新鲜度排序：互动率÷(发布天数+2)^1.5，新视频自动顶升</span>
    </div>
    <div class="video-list" id="group-fresh" style="margin-bottom:20px">
{fresh_cards if fresh_cards else '<div class="empty" style="padding:16px">暂无7天内新发布视频</div>'}
    </div>

    <!-- 📚 历史榜单 -->
    <div class="group-header" id="group-history-header" style="border-color:#f59e0b">
      📚 历史榜单 · 7-30天
      <span class="group-count" style="background:#fef3c7;color:#92400e">{len(history_rec)} 条</span>
      <span style="font-size:.74em;font-weight:400;color:#aaa">按综合评分排序 · 稳定内容持续追踪</span>
    </div>
    <div class="video-list" id="group-history">
{history_cards if history_cards else '<div class="empty" style="padding:16px">暂无7-30天历史视频</div>'}
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

<!-- PAGE 5: 投放效果 -->
<div class="page" id="page-promo">
  <!-- 添加投放追踪 -->
  <div class="promo-add-section">
    <div class="promo-add-title">添加投放追踪</div>
    <div class="promo-add-tabs">
      <div class="add-tab active" onclick="switchAddTab(this,'add-from-bv')">输入BV号（自动拉取）</div>
      <div class="add-tab" onclick="switchAddTab(this,'add-manual')">手动填写数据</div>
    </div>
    <div class="add-panel active" id="add-from-bv">
      <div class="add-form-row">
        <div class="add-form-field" style="max-width:280px">
          <label>BV号</label>
          <input id="add-bvid" type="text" placeholder="BV1xxxxxxxxx" oninput="onAddBvInput()" />
        </div>
        <div class="add-form-field" style="max-width:140px">
          <label>投放金额（元）</label>
          <input id="add-budget" type="number" placeholder="3000" min="0" />
        </div>
        <div class="add-form-field" style="max-width:200px">
          <label>投放开始时间</label>
          <input id="add-start-time" type="datetime-local" />
        </div>
        <div class="add-form-field" style="max-width:180px">
          <label>备注（选填）</label>
          <input id="add-note" type="text" placeholder="例：周末测试" />
        </div>
        <button class="btn btn-primary" style="height:36px;align-self:flex-end" onclick="submitFromBv()">+ 添加追踪</button>
      </div>
      <div id="add-bv-preview" style="font-size:.82em;color:#888;min-height:18px"></div>
      <div id="add-status" style="font-size:.82em;color:#3b6eea;min-height:18px;margin-top:4px"></div>
    </div>
    <div class="add-panel" id="add-manual">
      <div class="add-form-row">
        <div class="add-form-field">
          <label>视频标题</label>
          <input id="man-title" type="text" placeholder="视频标题" />
        </div>
        <div class="add-form-field" style="max-width:180px">
          <label>BV号（选填）</label>
          <input id="man-bvid" type="text" placeholder="BV1xxxxxxxxx" />
        </div>
        <div class="add-form-field" style="max-width:150px">
          <label>UP主名</label>
          <input id="man-author" type="text" placeholder="UP主昵称" />
        </div>
      </div>
      <div class="add-form-row">
        <div class="add-form-field" style="max-width:140px">
          <label>投放前播放量</label>
          <input id="man-play-before" type="number" placeholder="10000" min="0" />
        </div>
        <div class="add-form-field" style="max-width:140px">
          <label>投放前点赞</label>
          <input id="man-like-before" type="number" placeholder="500" min="0" />
        </div>
        <div class="add-form-field" style="max-width:140px">
          <label>投放金额（元）</label>
          <input id="man-budget" type="number" placeholder="3000" min="0" />
        </div>
        <div class="add-form-field" style="max-width:200px">
          <label>投放开始时间</label>
          <input id="man-start-time" type="datetime-local" />
        </div>
        <div class="add-form-field" style="max-width:180px">
          <label>备注（选填）</label>
          <input id="man-note" type="text" placeholder="" />
        </div>
        <button class="btn btn-primary" style="height:36px;align-self:flex-end" onclick="submitManual()">+ 添加</button>
      </div>
      <div id="man-status" style="font-size:.82em;color:#3b6eea;min-height:18px;margin-top:4px"></div>
    </div>
  </div>
  <!-- 投放效果看板 -->
  <div class="card">
    <div class="card-title">
      投放效果看板
      <span style="font-size:.78em;font-weight:400;color:#aaa">每次巡检自动更新 · CPM = 投入 ÷ 新增播放 × 1000</span>
    </div>
    <div class="card-sub">记录存储于 GitHub promotions.json · 自动追踪播放/点赞/互动增量</div>
    <div class="promo-list" id="promo-list-container">
      <div class="empty">加载中...</div>
    </div>
  </div>
</div>

<!-- PAGE 6: BV查询 -->
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
  <div class="card" style="margin-top:14px">
    <div class="card-title">GitHub Token 配置</div>
    <div class="card-sub">Token 存于浏览器本地，用于投放记录的云端同步</div>
    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:8px">
      <div id="token-status" style="font-size:.88em;color:#aaa;flex:1">检测中...</div>
      <button class="btn btn-secondary btn-sm" onclick="resetToken()">重置 Token</button>
    </div>
    <div style="font-size:.78em;color:#aaa;margin-top:8px">
      看板地址: <a href="https://jiahongsun675-del.github.io/lixiang-dashboard/" target="_blank" style="color:#3b6eea">https://jiahongsun675-del.github.io/lixiang-dashboard/</a>
    </div>
  </div>
</div>
<div class="modal-overlay" id="promo-modal">
  <div class="modal">
    <h3>标记加热 & 添加投放追踪</h3>
    <div class="modal-video-title" id="modal-video-title"></div>
    <div class="modal-field"><label>BV号</label><input id="modal-bvid" type="text" readonly style="background:#f8f9fc"/></div>
    <div class="modal-field"><label>投放金额（元，选填）</label><input id="modal-budget" type="number" placeholder="不填则仅标记加热，不计入投放追踪" min="0"></div>
    <div class="modal-field"><label>投放开始时间</label><input id="modal-start-time" type="datetime-local"/></div>
    <div class="modal-field"><label>备注（选填）</label><input id="modal-note" placeholder="例如：周末投放测试"></div>
    <div id="modal-fetch-status" style="font-size:.8em;color:#3b6eea;min-height:16px;margin-top:4px"></div>
    <div class="modal-actions">
      <button class="modal-cancel" onclick="closeModal()">取消</button>
      <button class="modal-submit" onclick="submitPromotion()">确认</button>
    </div>
  </div>
</div>

<script>
const TITLE_BLACKLIST = ['停车','剐蹭','刮蹭','车位','乱停','陷车','停车场','停车位','火车','理想进行曲','铁路','高铁','动车','offer','求职','招聘','应届','算法岗','大厂','字节'];
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
function setTimeFilter(btn, mode){{
  document.querySelectorAll('.filter-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  const maxH = mode==='3d'?72 : mode==='7d'?168 : 99999;
  document.querySelectorAll('.video-item[data-age-h]').forEach(el=>{{
    const ageH = parseFloat(el.dataset.ageH||99999);
    if(ageH <= maxH) el.classList.remove('time-hidden');
    else el.classList.add('time-hidden');
  }});
}}
function getHeatedSet(){{try{{return new Set(JSON.parse(localStorage.getItem('heated_bvids')||'[]'));}}catch{{return new Set();}}}}
function saveHeatedSet(s){{localStorage.setItem('heated_bvids',JSON.stringify([...s]));}}
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
document.getElementById('promo-modal').addEventListener('click',e=>{{if(e.target===document.getElementById('promo-modal'))closeModal();}});

// ── GitHub API ──
const GH_OWNER='jiahongsun675-del', GH_REPO='lixiang-dashboard', GH_BRANCH='main';
function getGhToken(){{return localStorage.getItem('lixiang_gh_token')||'';}}
function setGhToken(t){{localStorage.setItem('lixiang_gh_token',t);checkTokenStatus();}}
async function ghPromoGet(){{
  const t=getGhToken(); if(!t)return[[], null];
  const r=await fetch(`https://api.github.com/repos/${{GH_OWNER}}/${{GH_REPO}}/contents/promotions.json?ref=${{GH_BRANCH}}`,{{headers:{{'Authorization':`token ${{t}}`,'Accept':'application/vnd.github.v3+json'}}}});
  if(!r.ok)return[[], null];
  const d=await r.json();
  try{{return[JSON.parse(atob(d.content.replace(/\\n/g,''))), d.sha];}}catch{{return[[],d.sha];}}
}}
async function ghPromoPut(records, sha, message){{
  const t=getGhToken(); if(!t)throw new Error('未配置 GitHub Token');
  const content=btoa(unescape(encodeURIComponent(JSON.stringify(records,null,2))));
  const payload={{message:message||'update promotions',content,branch:GH_BRANCH}};
  if(sha)payload.sha=sha;
  const r=await fetch(`https://api.github.com/repos/${{GH_OWNER}}/${{GH_REPO}}/contents/promotions.json`,{{method:'PUT',headers:{{'Authorization':`token ${{t}}`,'Accept':'application/vnd.github.v3+json','Content-Type':'application/json'}},body:JSON.stringify(payload)}});
  if(!r.ok){{const e=await r.json();throw new Error(e.message||r.statusText);}}
}}

// ── 投放效果看板渲染 ──
const fmtNum = n => n>=10000?(n/10000).toFixed(1)+'万':Number(n||0).toLocaleString();
const fmtDate = iso => {{try{{return new Date(iso).toLocaleString('zh-CN',{{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}});}}catch{{return iso;}}}};

async function renderPromoList(){{
  const container=document.getElementById('promo-list-container');
  if(!container)return;
  if(!getGhToken()){{container.innerHTML='<div class="empty">请先在「监控配置」页配置 GitHub Token，才能加载投放记录</div>';return;}}
  container.innerHTML='<div class="empty">⏳ 加载中...</div>';
  try{{
    const[records]=await ghPromoGet();
    if(!records||!records.length){{container.innerHTML='<div class="empty">暂无投放记录 — 点击视频卡片右下角「标记加热」按钮登记</div>';return;}}
    // 并发拉取最新数据
    const active=records.filter(r=>r.status==='tracking'&&r.bvid&&!r.bvid.startsWith('MANUAL_'));
    const latestMap={{}};
    await Promise.all(active.map(async r=>{{
      try{{
        const res=await fetch(`https://api.bilibili.com/x/web-interface/view?bvid=${{r.bvid}}`);
        const d=await res.json();
        if(d.code===0)latestMap[r.bvid]=d.data.stat;
      }}catch{{}}
    }}));
    container.innerHTML=records.map(r=>renderPromoCard(r,latestMap[r.bvid])).join('');
  }}catch(e){{container.innerHTML=`<div class="empty">加载失败：${{e.message}}</div>`;}}
}}

function renderPromoCard(r, latest){{
  const bl=r.baseline; const latest_play=latest?latest.view:null;
  const delta_play=bl&&latest_play?latest_play-bl.play:null;
  const delta_like=bl&&latest?(latest.like-bl.like):null;
  const cpm=delta_play&&r.budget&&delta_play>0?(r.budget/delta_play*1000).toFixed(2):null;
  const progress=bl&&latest_play?Math.min(100,(delta_play/Math.max(bl.play*0.1,1)*100)):0;
  const statusBadge=r.status==='done'?'<span class="badge badge-heated">已完成</span>':'<span class="badge badge-growing">追踪中</span>';
  const deltaTxt=delta_play!=null?`+${{fmtNum(delta_play)}}`:'—';
  const rows=[
    ['播放量', bl?fmtNum(bl.play):'—', latest_play?fmtNum(latest_play):'—', delta_play!=null?`<span class="delta">${{deltaTxt}}</span>`:'—'],
    ['点赞', bl?fmtNum(bl.like):'—', latest&&latest.like?fmtNum(latest.like):'—', delta_like!=null?`<span class="delta">+${{fmtNum(delta_like)}}</span>`:'—'],
  ].map(([l,b,a,d])=>`<tr><td>${{l}}</td><td>${{b}}</td><td>${{a}}</td><td>${{d}}</td></tr>`).join('');
  return `<div class="promo-card-v2">
    <div class="promo-card-header">
      <div>
        <div class="promo-card-title"><a href="https://www.bilibili.com/video/${{r.bvid||''}}" target="_blank">${{r.title||r.bvid||'—'}}</a> ${{statusBadge}}</div>
        <div class="promo-card-meta">UP主: ${{r.author||'—'}} | BV: ${{r.bvid||'手动'}} | 投放时间: ${{fmtDate(r.start_time)}}${{r.note?' | '+r.note:''}}</div>
      </div>
      <div class="promo-card-budget"><div class="pcb-lbl">投放预算</div><div class="pcb-val">¥${{(r.budget||0).toLocaleString()}}</div></div>
    </div>
    ${{bl?`<div class="promo-track-bar"><div class="ptb-label">播放增量进度（相较投前基线）</div><div class="ptb-bar"><div class="ptb-fill" style="width:${{progress}}%"></div></div></div>`:'<div class="promo-waiting">⏳ 等待系统采集基线数据（下次巡检时自动记录）</div>'}}
    <table class="promo-compare-table"><thead><tr><th>指标</th><th>投放前</th><th>当前</th><th>增量</th></tr></thead><tbody>${{rows}}</tbody></table>
    ${{cpm?`<div class="promo-roi-row"><span class="roi-lbl">CPM（千次播放成本）</span><span class="roi-val">¥${{cpm}}</span><span style="font-size:.78em;color:#aaa;margin-left:8px">= ¥${{(r.budget||0).toLocaleString()}} / ${{delta_play?fmtNum(delta_play):'—'}} × 1000</span></div>`:''}}
    <div style="display:flex;gap:8px;margin-top:10px">
      <button class="btn btn-secondary btn-sm" onclick="stopPromo('${{r.bvid||''}}')">${{r.status==='done'?'重新追踪':'停止追踪'}}</button>
    </div>
  </div>`;
}}

async function stopPromo(bvid){{
  if(!bvid)return;
  const[records,sha]=await ghPromoGet();
  const idx=records.findIndex(r=>r.bvid===bvid);
  if(idx>=0){{records[idx].status=records[idx].status==='done'?'tracking':'done';}}
  await ghPromoPut(records,sha,'更新追踪状态: '+bvid);
  renderPromoList();
}}

// ── 从榜单「标记加热」弹窗 ──
function markHeated(bvid,title,author,score,budget){{
  const s=getHeatedSet();s.add(bvid);saveHeatedSet(s);
  const records=getLocalHeatedRecords();
  if(!records.find(r=>r.bvid===bvid)){{records.unshift({{bvid,title,author,score,budget,heated_at:new Date().toISOString()}});localStorage.setItem('heated_records',JSON.stringify(records));}}
  refreshHeatedUI();
  // 打开弹窗
  document.getElementById('modal-bvid').value=bvid;
  document.getElementById('modal-video-title').textContent=title+' · UP主: '+author;
  const now=new Date();now.setMinutes(now.getMinutes()-now.getTimezoneOffset());
  document.getElementById('modal-start-time').value=now.toISOString().slice(0,16);
  document.getElementById('modal-budget').value='';
  document.getElementById('modal-note').value='';
  document.getElementById('modal-fetch-status').textContent='';
  document.getElementById('promo-modal').classList.add('show');
}}

async function submitPromotion(){{
  const bvid=document.getElementById('modal-bvid').value.trim();
  const budget=parseFloat(document.getElementById('modal-budget').value)||0;
  const startTime=document.getElementById('modal-start-time').value;
  const note=document.getElementById('modal-note').value.trim();
  const status=document.getElementById('modal-fetch-status');
  const titleText=document.getElementById('modal-video-title').textContent;
  const upMatch=titleText.match(/UP主:\s*(.+)$/);
  const author=upMatch?upMatch[1].trim():'';
  const title=upMatch?titleText.replace(/\s*·\s*UP主:.*$/,'').trim():titleText.trim();
  if(budget>0){{
    if(!getGhToken()){{const t=prompt('请输入 GitHub Token：','');if(!t){{status.style.color='#dc2626';status.textContent='❌ 未提供Token';return;}}setGhToken(t);}}
    status.textContent='⏳ 保存追踪记录...';
    try{{
      const[records,sha]=await ghPromoGet();
      // 抓取当前数据作为基线
      let baseline=null;
      try{{const res=await fetch(`https://api.bilibili.com/x/web-interface/view?bvid=${{bvid}}`);const d=await res.json();if(d.code===0){{const st=d.data.stat;baseline={{time:startTime,play:st.view,like:st.like,coin:st.coin,favorite:st.favorite,reply:st.reply,share:st.share}};}}}}catch{{}}
      const newRec={{bvid,title:title||bvid,author,budget,start_time:startTime,note,created_at:new Date().toISOString(),baseline,snapshots:[],status:'tracking'}};
      const idx=records.findIndex(r=>r.bvid===bvid);
      if(idx>=0)records[idx]={{...records[idx],...newRec,baseline,snapshots:[],status:'tracking'}};
      else records.push(newRec);
      await ghPromoPut(records,sha,`登记投放: ${{bvid}} ¥${{budget}}`);
      status.style.color='#059669';status.textContent='✅ 已登记！基线数据已采集，监控自动追踪中';
    }}catch(e){{status.style.color='#dc2626';status.textContent='❌ 登记失败：'+e.message;return;}}
  }}else{{
    status.style.color='#059669';status.textContent='✅ 已标记加热（未填金额，不计入投放追踪）';
  }}
  setTimeout(()=>closeModal(),2000);
}}

async function onAddBvInput(){{
  const bvid=document.getElementById('add-bvid').value.trim().match(/BV[a-zA-Z0-9]+/)?.[0];
  const preview=document.getElementById('add-bv-preview');
  if(!bvid){{preview.textContent='';return;}}
  preview.textContent='⏳ 拉取视频信息...';
  try{{
    const res=await fetch(`https://api.bilibili.com/x/web-interface/view?bvid=${{bvid}}`);
    const d=await res.json();
    if(d.code===0){{const v=d.data;preview.textContent=`✓ ${{v.title}} · UP主: ${{v.owner.name}} · 播放: ${{fmtNum(v.stat.view)}}`;}}
    else{{preview.textContent='⚠ 视频不存在或无法访问';}}
  }}catch{{preview.textContent='';}}
}}

async function submitFromBv(){{
  const bvid=document.getElementById('add-bvid').value.trim().match(/BV[a-zA-Z0-9]+/)?.[0];
  const budget=parseFloat(document.getElementById('add-budget').value)||0;
  const startTime=document.getElementById('add-start-time').value;
  const note=document.getElementById('add-note').value.trim();
  const status=document.getElementById('add-status');
  if(!bvid){{status.style.color='#dc2626';status.textContent='❌ 请输入有效BV号';return;}}
  if(!startTime){{status.style.color='#dc2626';status.textContent='❌ 请选择投放开始时间';return;}}
  if(!getGhToken()){{const t=prompt('请输入 GitHub Token：','');if(!t){{status.style.color='#dc2626';status.textContent='❌ 未提供Token';return;}}setGhToken(t);}}
  status.textContent='⏳ 拉取视频信息并登记...';
  try{{
    const res=await fetch(`https://api.bilibili.com/x/web-interface/view?bvid=${{bvid}}`);
    const d=await res.json();
    const title=d.code===0?d.data.title:bvid;
    const author=d.code===0?d.data.owner.name:'';
    let baseline=null;
    if(d.code===0){{const st=d.data.stat;baseline={{time:startTime,play:st.view,like:st.like,coin:st.coin,favorite:st.favorite,reply:st.reply,share:st.share}};}}
    const[records,sha]=await ghPromoGet();
    const newRec={{bvid,title,author,budget,start_time:startTime,note,created_at:new Date().toISOString(),baseline,snapshots:[],status:'tracking'}};
    const idx=records.findIndex(r=>r.bvid===bvid);
    if(idx>=0)records[idx]={{...records[idx],...newRec,baseline,snapshots:[],status:'tracking'}};
    else records.push(newRec);
    await ghPromoPut(records,sha,`登记投放: ${{bvid}} ¥${{budget}}`);
    status.style.color='#059669';status.textContent='✅ 已登记！';
    document.getElementById('add-bvid').value='';document.getElementById('add-budget').value='';document.getElementById('add-bv-preview').textContent='';
    renderPromoList();
  }}catch(e){{status.style.color='#dc2626';status.textContent='❌ '+e.message;}}
}}

async function submitManual(){{
  const title=document.getElementById('man-title').value.trim();
  const bvid=document.getElementById('man-bvid').value.trim().match(/BV[a-zA-Z0-9]+/)?.[0]||('MANUAL_'+Date.now());
  const author=document.getElementById('man-author').value.trim();
  const playBefore=parseInt(document.getElementById('man-play-before').value)||0;
  const likeBefore=parseInt(document.getElementById('man-like-before').value)||0;
  const budget=parseFloat(document.getElementById('man-budget').value)||0;
  const startTime=document.getElementById('man-start-time').value;
  const note=document.getElementById('man-note').value.trim();
  const status=document.getElementById('man-status');
  if(!title){{status.style.color='#dc2626';status.textContent='❌ 请填写视频标题';return;}}
  if(!startTime){{status.style.color='#dc2626';status.textContent='❌ 请选择投放开始时间';return;}}
  if(!getGhToken()){{const t=prompt('请输入 GitHub Token：','');if(!t){{status.style.color='#dc2626';status.textContent='❌ 未提供Token';return;}}setGhToken(t);}}
  status.textContent='⏳ 保存...';
  const baseline=playBefore>0?{{time:startTime,play:playBefore,like:likeBefore,coin:0,favorite:0,reply:0,share:0}}:null;
  try{{
    const[records,sha]=await ghPromoGet();
    records.push({{bvid,title,author,budget,start_time:startTime,note,created_at:new Date().toISOString(),baseline,snapshots:[],status:'tracking',manual:true}});
    await ghPromoPut(records,sha,`手动登记: ${{title}}`);
    status.style.color='#059669';status.textContent='✅ 已登记！';
    ['man-title','man-bvid','man-author','man-play-before','man-like-before','man-budget','man-note'].forEach(id=>document.getElementById(id).value='');
    renderPromoList();
  }}catch(e){{status.style.color='#dc2626';status.textContent='❌ '+e.message;}}
}}

function switchAddTab(tab,panelId){{
  document.querySelectorAll('.add-tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.add-panel').forEach(p=>p.classList.remove('active'));
  tab.classList.add('active');document.getElementById(panelId).classList.add('active');
}}

function checkTokenStatus(){{
  const t=getGhToken();const el=document.getElementById('token-status');
  if(!el)return;
  if(t){{el.style.color='#059669';el.textContent='✅ Token 已配置（'+t.slice(0,8)+'...）';}}
  else{{el.style.color='#aaa';el.textContent='未配置，首次使用投放功能时会提示输入';}}
}}
function resetToken(){{if(confirm('确认清除已保存的 GitHub Token？')){{localStorage.removeItem('lixiang_gh_token');checkTokenStatus();}}}}

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
  const now=new Date();now.setMinutes(now.getMinutes()-now.getTimezoneOffset());
  const nowStr=now.toISOString().slice(0,16);
  const addStart=document.getElementById('add-start-time');if(addStart)addStart.value=nowStr;
  const manStart=document.getElementById('man-start-time');if(manStart)manStart.value=nowStr;
  applyBlacklistFilter();
  refreshHeatedUI();
  checkTokenStatus();
  renderPromoList();
}})();
</script>
</body>
</html>"""
    return html


# ══════════════════════════════════════════════
# 5b. 投放追踪自动快照更新
# ══════════════════════════════════════════════

async def auto_update_promo_snapshots(token: str):
    """每次巡检时自动更新 promotions.json 中的快照数据"""
    if not token:
        return
    # 读取 promotions.json
    import base64
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    api_url = f"https://api.github.com/repos/jiahongsun675-del/lixiang-dashboard/contents/promotions.json?ref=main"
    try:
        import urllib.request, json as _json
        req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read())
        content = _json.loads(base64.b64decode(data["content"].replace("\n", "")).decode())
        sha = data["sha"]
    except Exception as e:
        print(f"[PROMO] 读取 promotions.json 失败: {e}")
        return

    active = [r for r in content if r.get("status") == "tracking"
              and r.get("bvid") and not r["bvid"].startswith("MANUAL_")]
    if not active:
        return

    print(f"[PROMO] 更新 {len(active)} 条追踪记录的快照...")
    connector = aiohttp.TCPConnector(limit=5, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        details = await asyncio.gather(
            *[fetch_video_detail(session, r["bvid"]) for r in active],
            return_exceptions=True
        )

    changed = False
    now_str = datetime.now().isoformat()
    for record, detail in zip(active, details):
        if not isinstance(detail, dict):
            continue
        stat = {
            "time": now_str,
            "play": detail.get("play", 0),
            "like": detail.get("like", 0),
            "coin": detail.get("coin", 0),
            "favorite": detail.get("favorite", 0),
            "reply": detail.get("reply", 0),
            "share": detail.get("share", 0),
        }
        # 设置基线（首次）
        if not record.get("baseline") and stat["play"] > 0:
            record["baseline"] = stat.copy()
            changed = True
        # 添加快照（仅数据变化时）
        snaps = record.setdefault("snapshots", [])
        if not snaps or snaps[-1].get("play") != stat["play"]:
            snaps.append(stat)
            if len(snaps) > 100:  # 最多保留100条快照
                snaps = snaps[-100:]
                record["snapshots"] = snaps
            changed = True

    if not changed:
        print("[PROMO] 无变化，跳过更新")
        return

    # 写回 GitHub
    try:
        encoded = base64.b64encode(
            json.dumps(content, ensure_ascii=False, indent=2).encode()
        ).decode()
        put_data = json.dumps({
            "message": f"Auto snapshot {now_str[:16]}",
            "content": encoded,
            "sha": sha,
            "branch": "main",
        }).encode()
        req2 = urllib.request.Request(
            "https://api.github.com/repos/jiahongsun675-del/lixiang-dashboard/contents/promotions.json",
            data=put_data,
            headers={**headers, "Content-Type": "application/json"},
            method="PUT",
        )
        with urllib.request.urlopen(req2, timeout=15):
            pass
        print(f"[PROMO] 快照已更新（{len(active)} 条）")
    except Exception as e:
        print(f"[PROMO] 写入 promotions.json 失败: {e}")




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
# 6b. 飞书定时推送
# ══════════════════════════════════════════════

def get_pushed_alerts() -> dict:
    """读取已推送记录（bvid → push_time）"""
    try:
        if PUSHED_ALERTS_FILE.exists():
            return json.loads(PUSHED_ALERTS_FILE.read_text())
    except Exception:
        pass
    return {}


def mark_pushed(bvid: str):
    """标记已推送"""
    records = get_pushed_alerts()
    records[bvid] = datetime.now().isoformat()
    # 只保留最近7天的记录
    cutoff = time.time() - 7 * 86400
    records = {k: v for k, v in records.items()
               if datetime.fromisoformat(v).timestamp() > cutoff}
    records[bvid] = datetime.now().isoformat()
    PUSHED_ALERTS_FILE.write_text(json.dumps(records, ensure_ascii=False))


def send_feishu_realtime(v: dict):
    """高价值内容实时推送（单条视频）"""
    import urllib.request, json as _json

    score  = v.get("total_score", 0)
    title  = v.get("title", "")
    author = v.get("author", "")
    fans   = v.get("fans", 0)
    play   = v.get("play", 0)
    ir     = v.get("interaction_rate", 0)
    bvid   = v.get("bvid", "")
    url    = f"https://www.bilibili.com/video/{bvid}"
    fans_s = f"{fans/10000:.1f}万粉" if fans > 0 else "粉丝未知"
    play_s = f"{play/10000:.1f}万" if play >= 10000 else str(play)

    if score >= 80:    priority, budget = "🔴 极高价值", "¥6,000~10,000"
    elif score >= 65:  priority, budget = "🟠 高价值",   "¥3,000~6,000"
    else:              priority, budget = "🟡 较高价值", "¥2,000~4,000"

    tags = []
    if v.get("age_h", 999) < 72: tags.append("🆕新内容")
    if play >= 50000:             tags.append("🔥高热度")
    if v.get("underdog_bonus"):   tags.append("⭐素人")

    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"⚡ 高价值内容预警 · 评分 {score}分"},
                "template": "red" if score >= 80 else "orange"
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"**{title}**\n\n"
                            f"UP主：{author}（{fans_s}） | {priority} {' '.join(tags)}\n"
                            f"播放：{play_s} | 互动率：{ir:.1f}% | 综合评分：**{score}分**\n"
                            f"建议预算：**{budget}**\n\n"
                            f"[→ 立即查看视频]({url})"
                        )
                    }
                },
                {"tag": "hr"},
                {
                    "tag": "action",
                    "actions": [{
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "查看完整看板"},
                        "url": DASHBOARD_URL,
                        "type": "primary"
                    }]
                }
            ]
        }
    }

    data = _json.dumps(payload, ensure_ascii=False).encode()
    try:
        req = urllib.request.Request(
            FEISHU_WEBHOOK, data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = _json.loads(resp.read())
        if result.get("code") == 0 or result.get("StatusCode") == 0:
            mark_pushed(bvid)
            print(f"[飞书实时] 已推送: {title[:30]} ({score}分)")
        else:
            print(f"[飞书实时] 推送失败: {result}")
    except Exception as e:
        print(f"[飞书实时] 推送异常: {e}")



def send_feishu_push(scored_videos: list, now_str: str):
    """向飞书机器人推送今日推荐内容（自动排除前一天已推送的视频）"""
    import urllib.request, json as _json

    # 过滤掉24小时内已推送过的视频
    pushed = get_pushed_alerts()
    cutoff_24h = time.time() - 86400
    recently_pushed = {
        bvid for bvid, push_time in pushed.items()
        if datetime.fromisoformat(push_time).timestamp() > cutoff_24h
    }

    top = sorted(
        [v for v in scored_videos
         if v.get("total_score", 0) >= SCORE_THRESHOLD
         and v.get("bvid") not in recently_pushed],
        key=lambda v: v["total_score"], reverse=True
    )[:10]

    if not top:
        print("[飞书] 无新视频可推（近24小时内已推过所有达标视频）")
        return

    def fmt_n(n):
        return f"{n/10000:.1f}万" if n >= 10000 else str(n)

    # 构造卡片消息
    lines = []
    for i, v in enumerate(top, 1):
        title = v.get("title", "")[:35] + ("…" if len(v.get("title","")) > 35 else "")
        author = v.get("author", "")
        score  = v.get("total_score", 0)
        play   = fmt_n(v.get("play", 0))
        ir     = v.get("interaction_rate", 0)
        fans   = v.get("fans", 0)
        fans_str = f"{fans/10000:.1f}万粉" if fans > 0 else "粉丝未知"

        # 预算
        if score >= 65:   budget = "¥6,000~10,000"
        elif score >= 50: budget = "¥3,000~6,000"
        elif score >= 40: budget = "¥2,000~4,000"
        else:             budget = "¥1,000~2,000"

        # 标签
        tags = []
        if v.get("age_h", 999) < 72:  tags.append("🆕新内容")
        if v.get("play", 0) >= 50000:  tags.append("🔥高热度")
        if v.get("underdog_bonus", 0):  tags.append("⭐素人")
        if v.get("is_stale"):           tags.append("🥶停滞")
        tag_str = " ".join(tags)

        url = f"https://www.bilibili.com/video/{v['bvid']}"
        lines.append(
            f"**#{i} {title}**\n"
            f"UP主：{author}（{fans_str}）| 评分：**{score}分** {tag_str}\n"
            f"播放：{play} | 互动率：{ir:.1f}% | 建议预算：{budget}\n"
            f"[→ 查看视频]({url})\n"
        )

    body_text = "\n".join(lines)
    total = len(scored_videos)
    recommend_cnt = len(top)

    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"🚗 理想汽车B站推荐日报 · {now_str[:10]}"},
                "template": "blue"
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"**本次分析：{total} 个视频 | 达标推荐：{recommend_cnt} 个**\n"
                            f"评分维度：互动率 × 增长趋势 × 发布时效 × 口碑评价\n"
                        )
                    }
                },
                {"tag": "hr"},
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": body_text}
                },
                {"tag": "hr"},
                {
                    "tag": "action",
                    "actions": [{
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "查看完整看板"},
                        "url": DASHBOARD_URL,
                        "type": "primary"
                    }]
                }
            ]
        }
    }

    data = _json.dumps(payload, ensure_ascii=False).encode()
    try:
        req = urllib.request.Request(
            FEISHU_WEBHOOK,
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = _json.loads(resp.read())
        if result.get("code") == 0 or result.get("StatusCode") == 0:
            # 标记已推送，避免下次重复
            for v in top:
                mark_pushed(v.get("bvid", ""))
            print(f"[飞书] 推送成功，共 {recommend_cnt} 条新推荐（已记录，明天不重复）")
        else:
            print(f"[飞书] 推送失败: {result}")
    except Exception as e:
        print(f"[飞书] 推送异常: {e}")


async def feishu_push_main():
    """独立飞书推送模式：抓取最新数据并推送"""
    print(f"[{datetime.now():%H:%M:%S}] 飞书定时推送...")
    raw_videos = await collect_all_videos()
    conn = init_db()
    filtered = [v for v in raw_videos
                if not is_blacklisted(v.get("title", ""))
                and v.get("play", 0) >= MIN_PLAY
                and v.get("fans", 0) >= MIN_FANS]   # 严格：粉丝数必须 ≥5万
    scored = []
    for v in filtered:
        sv = score_video(v, get_prev_play(conn, v["bvid"]))
        save_snapshot(conn, sv)
        scored.append(sv)
    conn.close()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    send_feishu_push(scored, now_str)
    # 顺便更新 dashboard
    if len(scored) >= 3:
        html = generate_html(scored, now_str)
        with open(HTML_PATH, "w", encoding="utf-8") as f:
            f.write(html)
        token = GITHUB_TOKEN
        if token:
            git_push(token, f"Feishu push update {now_str}")




async def deep_crawl_main(minutes: int = 30):
    """
    深度采集模式：持续运行指定分钟数，最大化覆盖内容。
    策略：
      - 首轮：全量深扫（5轮 × 15页）
      - 后续：每60s抓一次新发布 + 热门，捕获期间新上传的内容
      - 每5分钟推送一次 GitHub，实时更新看板
    """
    deadline  = time.time() + minutes * 60
    push_every = 300  # 每5分钟推送一次
    last_push  = 0

    conn_db = init_db()
    all_seen: dict = {}  # bvid → video_dict（全局去重）
    token = GITHUB_TOKEN or os.environ.get("GITHUB_TOKEN", "")

    print(f"\n{'='*60}")
    print(f"深度采集模式：持续 {minutes} 分钟")
    print(f"截止时间: {datetime.fromtimestamp(deadline):%H:%M:%S}")
    print(f"{'='*60}")

    # ── 首轮：全量深扫 ──
    print(f"\n[{datetime.now():%H:%M:%S}] 首轮全量深扫...")
    full_videos = await collect_all_videos()
    for v in full_videos:
        bvid = v.get("bvid", "")
        if bvid and bvid not in all_seen:
            all_seen[bvid] = v
    print(f"  首轮完成，累计 {len(all_seen)} 个视频")

    # ── 生成并推送第一版 ──
    await _score_and_push(all_seen, conn_db, token, "首轮全量扫描")
    last_push = time.time()

    batch_num = 1
    # ── 持续循环：抓新内容 ──
    while time.time() < deadline:
        remaining = int(deadline - time.time())
        wait = min(60, remaining)
        if wait <= 0:
            break
        print(f"\n[{datetime.now():%H:%M:%S}] 等待 {wait}s 后第 {batch_num+1} 批...（剩余 {remaining}s）")
        await asyncio.sleep(wait)

        batch_num += 1
        connector = aiohttp.TCPConnector(limit=15, ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            tasks = []
            # 重点：pubdate 最新内容（捕获新上传）
            for kw in KEYWORDS:
                tasks.append(search_videos(session, kw, order="pubdate", page=1))
                tasks.append(search_videos(session, kw, order="pubdate", page=2))
            # totalrank 综合榜（排名可能变化）
            for kw in KEYWORDS[:5]:
                tasks.append(search_videos(session, kw, order="totalrank", page=1))
            # 汽车分区最新
            tasks.append(fetch_newlist(session, rid=17, ps=50, pn=1))
            # UP主频道新投稿
            for mid in TOP_CREATOR_MIDS:
                tasks.append(fetch_up_videos(session, mid, ps=10))

            results = await asyncio.gather(*tasks, return_exceptions=True)
            new_count = 0
            for batch in results:
                if isinstance(batch, Exception) or not batch:
                    continue
                for v in batch:
                    bvid = v.get("bvid", "")
                    if bvid and bvid not in all_seen:
                        all_seen[bvid] = v
                        new_count += 1
                    elif bvid and all_seen.get(bvid, {}).get("play", 0) < v.get("play", 0):
                        all_seen[bvid].update(v)

        print(f"  第{batch_num}批 +{new_count} 新视频，累计 {len(all_seen)} 个")

        # 每5分钟推送一次
        if time.time() - last_push >= push_every:
            await _score_and_push(all_seen, conn_db, token, f"第{batch_num}批更新")
            last_push = time.time()

    # ── 最终推送 ──
    print(f"\n[{datetime.now():%H:%M:%S}] 时间到，生成最终版本...")
    await _score_and_push(all_seen, conn_db, token, f"深度采集完成 {minutes}min")
    conn_db.close()
    print(f"\n深度采集结束！共 {len(all_seen)} 个视频，{batch_num} 批次")
    print(f"{'='*60}\n")


async def _score_and_push(all_seen: dict, conn_db, token: str, label: str):
    """对已采集视频评分、过滤、生成 HTML 并推送"""
    raw_list = list(all_seen.values())

    # 过滤
    filtered = [v for v in raw_list
                if not is_blacklisted(v.get("title", ""))
                and v.get("play", 0) >= MIN_PLAY
                and v.get("fans", 0) >= MIN_FANS]   # 严格：粉丝数必须 ≥5万

    # 评分
    scored = []
    for v in filtered:
        prev_play = get_prev_play(conn_db, v["bvid"])
        sv = score_video(v, prev_play)
        save_snapshot(conn_db, sv)
        scored.append(sv)

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    recommend_cnt = sum(1 for v in scored if v["total_score"] >= SCORE_THRESHOLD)

    if len(scored) < 3:
        print(f"  [{label}] 视频数不足，跳过")
        return

    html = generate_html(scored, now_str)
    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  [{label}] 生成 index.html ({len(html)//1024}KB)，推荐榜 {recommend_cnt} 条")

    if token:
        git_push(token, f"Deep crawl [{label}] {now_str} ({len(scored)} videos)")
    if token:
        await auto_update_promo_snapshots(token)


async def backfill_fan_counts():
    """补全 DB 中 fans=0 的视频粉丝数（每次运行时顺带修复）"""
    conn = init_db()
    c = conn.cursor()
    c.execute("""
        SELECT DISTINCT mid FROM video_meta
        WHERE fans = 0 AND mid > 0
        LIMIT 50
    """)
    zero_mids = [row[0] for row in c.fetchall()]
    if not zero_mids:
        conn.close()
        return
    print(f"[补全] 修复 {len(zero_mids)} 个 fans=0 的 UP 主粉丝数...")
    fan_connector = aiohttp.TCPConnector(limit=10, ssl=False)
    async with aiohttp.ClientSession(connector=fan_connector) as session:
        results = []
        for i in range(0, len(zero_mids), 10):
            batch = zero_mids[i:i+10]
            br = await asyncio.gather(
                *[fetch_fan_count(session, m) for m in batch],
                return_exceptions=True
            )
            results.extend(br)
            if i + 10 < len(zero_mids):
                await asyncio.sleep(0.3)
    updated = 0
    for mid, f in zip(zero_mids, results):
        fans = f if isinstance(f, int) and f > 0 else 0
        if fans > 0:
            c.execute("UPDATE video_meta SET fans=? WHERE mid=?", (fans, mid))
            save_fan_cache(conn, mid, fans)
            updated += 1
    conn.commit()
    conn.close()
    print(f"[补全] 已修复 {updated}/{len(zero_mids)} 个 UP 主粉丝数")


async def main():
    start = time.time()
    print(f"\n{'='*60}")
    print(f"理想汽车B站监控 - 多源异步采集")
    print(f"启动时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}")

    # Step 0: 补全历史 fans=0（最多50个，后台顺带修复）
    await backfill_fan_counts()

    # Step 1: 采集
    raw_videos = await collect_all_videos()

    # Step 2: 过滤（黑名单 + 播放量 + 粉丝数）
    # 严格模式：粉丝数未知(0)视为不达标，一律过滤
    filtered = [v for v in raw_videos
                if not is_blacklisted(v.get("title", ""))
                and v.get("play", 0) >= MIN_PLAY
                and v.get("fans", 0) >= MIN_FANS]   # 严格：粉丝数必须 ≥5万
    print(f"过滤后: {len(filtered)} 个视频 (黑名单/低播放/低粉丝已排除)")

    # Step 3: 增量存储 + 评分（本次抓取）
    conn = init_db()
    scored_this_run = []
    changed_count = 0
    this_run_bvids = set()
    for v in filtered:
        prev_play = get_prev_play(conn, v["bvid"])
        sv = score_video(v, prev_play)
        if save_snapshot(conn, sv):
            changed_count += 1
        scored_this_run.append(sv)
        this_run_bvids.add(v["bvid"])
    print(f"数据变化: {changed_count}/{len(filtered)} 个视频有更新")

    # Step 3b: 从 DB 补入30天内历史高质量视频（让榜单保持丰富）
    cutoff_30d = (datetime.now().timestamp() - 30 * 86400)
    c = conn.cursor()
    c.execute("""
        SELECT m.bvid, m.title, m.author, m.mid, m.fans, m.pubdate, m.last_score,
               s.play, s.like_count, s.coin, s.favorite, s.reply, s.danmaku
        FROM video_meta m
        LEFT JOIN (
            SELECT bvid, MAX(snapshot_time) mt FROM video_snapshots GROUP BY bvid
        ) latest ON latest.bvid = m.bvid
        LEFT JOIN video_snapshots s ON s.bvid = m.bvid AND s.snapshot_time = latest.mt
        WHERE m.fans >= ? AND m.last_score >= ?
          AND m.pubdate >= ?
          AND m.bvid NOT IN ({})
        ORDER BY m.last_score DESC
        LIMIT 200
    """.format(','.join('?' * len(this_run_bvids)) if this_run_bvids else '""'),
    [MIN_FANS, SCORE_THRESHOLD, int(cutoff_30d)] + list(this_run_bvids))
    db_rows = c.fetchall()
    conn.close()

    db_videos = []
    for row in db_rows:
        bvid, title, author, mid, fans, pubdate, last_score, play, like, coin, fav, reply, danmaku = row
        if not title or is_blacklisted(title):
            continue
        v = {
            "bvid": bvid, "title": title, "author": author, "mid": mid or 0,
            "fans": fans or 0, "pubdate": pubdate or 0,
            "play": play or 0, "like": like or 0, "coin": coin or 0,
            "favorite": fav or 0, "reply": reply or 0, "danmaku": danmaku or 0,
        }
        sv = score_video(v, None)
        db_videos.append(sv)

    scored = scored_this_run + db_videos
    print(f"合并DB历史: 本次 {len(scored_this_run)} + DB补充 {len(db_videos)} = {len(scored)} 个视频")

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

    # Step 5b: 高价值内容实时飞书推送
    pushed = get_pushed_alerts()
    new_high = [v for v in scored
                if v.get("total_score", 0) >= HIGH_VALUE_THRESHOLD
                and v.get("bvid") not in pushed]
    if new_high:
        print(f"[飞书实时] 发现 {len(new_high)} 条高价值内容（≥{HIGH_VALUE_THRESHOLD}分），逐条推送...")
        for v in new_high:
            send_feishu_realtime(v)
    else:
        print(f"[飞书实时] 无新高价值内容（阈值 {HIGH_VALUE_THRESHOLD}分）")

    # Step 6: 自动更新投放追踪快照
    if token:
        await auto_update_promo_snapshots(token)

    elapsed = time.time() - start
    print(f"\n完成！耗时 {elapsed:.1f}s，推荐榜 {sum(1 for v in scored if v['total_score']>=SCORE_THRESHOLD)} 个视频")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    if "--deep" in args:
        idx = args.index("--deep")
        minutes = int(args[idx + 1]) if idx + 1 < len(args) else 30
        asyncio.run(deep_crawl_main(minutes))
    elif "--feishu-push" in args:
        asyncio.run(feishu_push_main())
    else:
        asyncio.run(main())

