"""闲鱼 Playwright 抓取器 — 独立脚本，用户本地运行。

用法:
    # 首次运行：扫码登录
    python src/crawler/xianyu_scraper.py --login

    # 后续抓取：自动用已保存的登录态
    python src/crawler/xianyu_scraper.py --search "杭州租房" --limit 20

    # 输出 JSON 到 stdout，可管道给 pipeline
    python src/crawler/xianyu_scraper.py --search "杭州租房" --json > listings.json

依赖: pip install playwright && python -m playwright install chromium
"""

import argparse
import json
import os
import re
import sys
import time
import random
from pathlib import Path

COOKIE_FILE = Path(__file__).parent / "xianyu_state.json"


def _log(msg: str):
    """写日志到 stderr，不污染 stdout 的 JSON 输出。"""
    print(msg, file=sys.stderr)


def get_browser(login: bool = False):
    """启动浏览器。login=True 时用有头模式方便扫码。"""
    from playwright.sync_api import sync_playwright
    p = sync_playwright().start()
    browser = p.chromium.launch(
        headless=not login,
        args=["--disable-blink-features=AutomationControlled"]
    )
    context = browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    )
    # 注入反检测脚本
    context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
        Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
    """)

    # 加载已保存的登录态
    if COOKIE_FILE.exists():
        state = json.loads(COOKIE_FILE.read_text())
        context.add_cookies(state.get("cookies", []))
        _log(f">> Login state loaded: {len(state.get('cookies', []))} cookies")

    return p, browser, context


def do_login():
    """手动扫码登录，保存登录态。"""
    print("\n>> Opening browser. Please scan QR code with Xianyu APP to login...")
    print("   Press Enter after login.\n")

    p, browser, context = get_browser(login=True)
    page = context.new_page()

    # 访问闲鱼首页触发登录
    page.goto("https://www.goofish.com/", wait_until="domcontentloaded")
    time.sleep(2)

    # 找登录按钮
    try:
        page.click('text=登录', timeout=5000)
    except Exception:
        pass  # 可能已经在登录页

    # 等用户扫码
    input("👉 扫码完成后按 Enter...")

    # 保存 cookie
    cookies = context.cookies()
    state = {"cookies": cookies, "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    COOKIE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    print(f"[OK]登录态已保存 ({len(cookies)} 个 cookie)")

    browser.close()
    p.stop()


def search_listings(keyword: str, limit: int = 20) -> list[dict]:
    """搜索闲鱼，返回商品链接列表。"""
    p, browser, context = get_browser()
    page = context.new_page()

    search_url = f"https://www.goofish.com/search?q={keyword}"
    _log(f"[Search]搜索: {keyword}")

    page.goto(search_url, wait_until="networkidle", timeout=30000)
    time.sleep(3)

    # 滚动加载更多
    for _ in range(3):
        page.evaluate("window.scrollBy(0, 800)")
        time.sleep(random.uniform(1.5, 3.0))

    # 提取商品链接
    links = page.evaluate("""() => {
        const items = document.querySelectorAll('a[href*="/item/"]');
        return Array.from(items).map(a => ({
            url: a.href,
            title: a.textContent.trim().substring(0, 100)
        }));
    }""")

    # 去重
    seen = set()
    unique = []
    for item in links:
        # 提取纯 URL（去掉 query string）
        url = re.sub(r'[?].*$', '', item["url"])
        if url not in seen:
            seen.add(url)
            unique.append({"url": url, "source": "闲鱼", "title": item["title"]})
            if len(unique) >= limit:
                break

    _log(f"   [OK]找到 {len(unique)} 个商品")
    browser.close()
    p.stop()
    return unique


def scrape_details_sequential(urls: list[str], context) -> list[dict]:
    """串行抓取详情页（复用同一个 page，避免 greenlet 跨线程问题）。

    Playwright 同步 API 的 greenlet 绑定创建线程，ThreadPoolExecutor 会导致
    greenlet.error: cannot switch to a different thread。
    改用串行 + domcontentloaded，每个 ~3 秒，30 个约 90 秒。
    """
    page = context.new_page()
    results = []
    try:
        for i, url in enumerate(urls):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(1500)
                text = page.inner_text("body")
                if text:
                    results.append({"url": url, "source": "闲鱼", "content": text[:4000]})
            except Exception:
                pass
            if (i + 1) % 5 == 0:
                _log(f"  [{i+1}/{len(urls)}]")
    finally:
        page.close()
    return results


def full_scrape(keyword: str, limit: int = 10, debug: bool = False):
    """完整抓取：搜索 + 详情页内容提取。"""
    _log(f"[Xianyu] Scraping: {keyword}")

    p, browser, context = get_browser()
    if debug:
        _log("[debug] Browser visible mode")

    # 搜索 — domcontentloaded 比 networkidle 更可靠
    page = context.new_page()
    page.set_default_timeout(45000)
    search_url = f"https://www.goofish.com/search?q={keyword}"
    try:
        page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        _log("[Warn] Page load timeout, continuing anyway...")
    time.sleep(5)  # 等搜索结果渲染

    # 滚动加载
    for i in range(5):
        page.evaluate("window.scrollBy(0, window.innerHeight)")
        time.sleep(random.uniform(1.0, 2.0))

    # 搜索商品 ID — 闲鱼是 SPA，商品不是 <a> 链接
    # 尝试多种方式：data-itemid、data-id、或者从 page content 提取
    item_ids = page.evaluate(r"""() => {
        let ids = Array.from(document.querySelectorAll('[data-itemid]')).map(el => el.getAttribute('data-itemid'));
        if (ids.length > 0) return ids;
        ids = Array.from(document.querySelectorAll('[data-id]')).map(el => el.getAttribute('data-id'));
        if (ids.length > 0) return ids;
        let hrefs = Array.from(document.querySelectorAll('a[href*="item"], a[href*="/id/"]')).map(a => a.href);
        if (hrefs.length > 0) return hrefs;
        let html = document.body.innerHTML;
        let matches = html.match(/itemId[":\s]+(\d+)/g) || html.match(/item[_-]?id[":\s]+(\d+)/gi) || [];
        return matches.map(m => m.replace(/[^0-9]/g, ''));
    }""")

    if debug:
        _log(f"[debug] Found {len(item_ids)} potential items: {item_ids[:5]}")

    # 构建商品 URL — 保留原始 URL 参数
    seen = set()
    urls = []
    for raw in item_ids:
        if not raw:
            continue
        raw_str = str(raw).strip()
        # 已经是完整 URL，直接使用
        if raw_str.startswith("http"):
            item_id = re.search(r'[?&]id=(\d{8,})', raw_str)
            item_id = item_id.group(1) if item_id else raw_str.split('/')[-1].split('?')[0]
            if item_id and item_id not in seen and item_id.isdigit():
                seen.add(item_id)
                urls.append(raw_str)  # 保留原始 URL
        elif raw_str.isdigit() and len(raw_str) >= 8:
            if raw_str not in seen:
                seen.add(raw_str)
                urls.append(f"https://www.goofish.com/item/{raw_str}")

        if len(urls) >= limit:
            break

    # fallback: HTML 中找 itemId
    if not urls:
        html = page.content()
        for mid in re.findall(r'"itemId"\s*:\s*"(\d{8,})"', html):
            if mid not in seen:
                seen.add(mid)
                urls.append(f"https://www.goofish.com/item/{mid}")
                if len(urls) >= limit:
                    break

    page.close()
    _log(f"  Found {len(urls)} product URLs")

    _log(f"[Link] {len(urls)} product URLs, scraping details...")

    # 串行抓取详情（复用 page，domcontentloaded 快速模式，~3s/条）
    results = scrape_details_sequential(urls, context)

    browser.close()
    p.stop()

    _log(f"[OK] Done: {len(results)}/{len(urls)} details")
    return results


# ——— CLI ———
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="闲鱼 Playwright 抓取器")
    parser.add_argument("--login", action="store_true", help="扫码登录并保存 cookie")
    parser.add_argument("--search", type=str, help="搜索关键词")
    parser.add_argument("--limit", type=int, default=10, help="最大条数")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--full", action="store_true", help="完整抓取（含详情页）")
    parser.add_argument("--output", type=str, help="直接写入 JSON 文件")
    parser.add_argument("--debug", action="store_true", help="可见浏览器+调试信息")
    args = parser.parse_args()

    if args.login:
        do_login()
    elif args.full and args.search:
        results = full_scrape(args.search, args.limit, debug=args.debug)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False, indent=2)
            _log(f"Saved {len(results)} listings to {args.output}")
        elif args.json:
            print(json.dumps(results, ensure_ascii=False, indent=2))
    elif args.search:
        items = search_listings(args.search, args.limit)
        if args.json:
            print(json.dumps(items, ensure_ascii=False, indent=2))
        else:
            for item in items:
                print(f"  {item['url']} | {item.get('title', '')[:50]}")
    else:
        parser.print_help()
