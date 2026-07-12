"""58同城抓取 — 多渠道尝试，自动降级。

移动版可能反爬，自动切 PC 端或其他入口。
"""

import re
import httpx

# 多个入口，按优先级尝试
WUBA_URLS = [
    "https://hz.58.com/zufang/0/",       # PC 端
    "https://hz.58.com/zufang/pn1/",      # PC 端分页
    "https://m.58.com/hz/zufang/",        # 移动版（可能反爬）
]

# 杭州各城区名
HZ_DISTRICTS = [
    "拱墅", "西湖", "余杭", "上城", "滨江", "萧山", "钱塘",
    "临平", "富阳", "临安", "桐庐", "淳安", "建德",
]

# 非房源 UI 文本
UI_NOISE = [
    r"^\d+元以下$", r"^\d+-\d+\s*元$", r"^\d+元以上$",
    r"不限", r"筛选", r"区域", r"地铁", r"位置", r"租金",
    r"户型", r"发布", r"我的", r"找房子", r"整租\s*合租",
    r"次卧", r"主卧", r"个人\s*经纪人", r"品牌公寓",
    r"验证", r"滑块", r"安全检测", r"callback", r"antibot",
]


async def _try_fetch(url: str, client: httpx.AsyncClient) -> str | None:
    """尝试抓取一个 URL，返回 HTML 文本或 None。"""
    try:
        resp = await client.get(url, headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://hz.58.com/",
        }, timeout=15, follow_redirects=True)
        if resp.status_code == 200 and len(resp.text) > 5000:
            # 检查是否被反爬拦截
            if "antibot" in resp.text.lower() or "验证" in resp.text[:500]:
                return None
            return resp.text
    except httpx.RequestError:
        pass
    return None


async def crawl_wuba_listings(limit: int = 20) -> list[dict]:
    """从 58同城杭州租房抓取房源列表。自动尝试多个入口。

    返回: [{"url": ..., "source": "58同城", "content": ...}, ...]
    """
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        html = None
        for url in WUBA_URLS:
            html = await _try_fetch(url, client)
            if html:
                break

    if not html:
        return []

    # 提取 body 文本
    body_m = re.search(r"<body[^>]*>(.*?)</body>", html, re.DOTALL)
    text = body_m.group(1) if body_m else html
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    # 按"元"分割
    segments = re.split(r"(?<=\d)\s*元\b", text)

    items = []
    seen = set()
    for i in range(len(segments) - 1):
        before = segments[i].strip()
        after = segments[i + 1].strip()

        content = before[-300:] + " 元 " + after[:100]
        content = re.sub(r"\b(?:安选|精选|优选|急售|新房|特价)\b", "", content)
        content = re.sub(r"\s+", " ", content).strip()

        # 跳过 UI / 反爬文本
        if any(re.search(p, content) for p in UI_NOISE):
            continue
        if "callback" in content.lower() or "antibot" in content.lower():
            continue

        # 必须含杭州城区或小区特征
        has_loc = any(d in content for d in HZ_DISTRICTS)
        has_comm = bool(re.search(r"[区路街苑花园城公寓]+\b", content))
        if not has_loc and not has_comm:
            continue

        key = content[:60]
        if len(content) > 30 and key not in seen:
            seen.add(key)
            items.append({
                "url": f"https://hz.58.com/zufang/#{i}",
                "source": "58同城",
                "content": content,
            })
            if len(items) >= limit:
                break

    return items
