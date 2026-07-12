"""小红书扫码登录 — 独立脚本，一行命令完成。

用法:
    export LD_LIBRARY_PATH=/tmp/chromium-libs/lib:$LD_LIBRARY_PATH
    python3 xhs_login.py

流程:
    1. headless 打开小红书 → 自动弹出 QR 码
    2. 把 QR 链接打印到终端
    3. 你在 Windows 浏览器打开链接，用手机 App 扫码
    4. 自动检测登录完成，保存 cookie
"""

import asyncio, json, time
from pathlib import Path
from playwright.async_api import async_playwright

ANTI_DETECT = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
    window.chrome = { runtime: {} };
"""

COOKIE_FILE = Path(__file__).parent / "src" / "crawler" / "xhs_state.json"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                  "--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            locale="zh-CN",
        )
        await ctx.add_init_script(ANTI_DETECT)
        page = await ctx.new_page()

        qr_url = None
        login_done = False

        async def on_resp(r):
            nonlocal qr_url, login_done
            try:
                d = json.loads(await r.text())
                if "qrcode/create" in r.url:
                    qr_url = d.get("data", {}).get("url", "")
                    if qr_url:
                        print(f"\n📱 复制这个链接到 Windows 浏览器打开，然后用小红书 App 扫码：\n")
                        print(f"   {qr_url}\n")
                        print("⏳ 等待扫码中...")
                elif "qrcode/userinfo" in r.url:
                    s = d.get("data", {}).get("codeStatus", -1)
                    if s == 2:
                        print("   👆 已扫码，请在手机上点确认...")
                    elif s == 3:
                        print("   ✅ 登录成功！")
                        login_done = True
            except Exception:
                pass

        page.on("response", on_resp)

        print("🌐 正在打开小红书...")
        await page.goto("https://www.xiaohongshu.com", timeout=30000, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        if not qr_url:
            print("   尝试点击登录按钮...")
            try:
                btn = await page.wait_for_selector('text=登录', timeout=5000)
                if btn:
                    await btn.click()
                    await page.wait_for_timeout(3000)
            except Exception:
                pass

        if not qr_url:
            print("❌ 未能获取 QR 码，请检查网络或重试")
            await browser.close()
            return

        # 等待登录
        for _ in range(120):
            if login_done:
                break
            await page.wait_for_timeout(1000)

        page.remove_listener("response", on_resp)

        if login_done:
            cookies = await context.cookies()
            state = {"cookies": cookies, "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}
            COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
            COOKIE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))
            print(f"\n🎉 完成！{len(cookies)} 个 cookie 已保存到 {COOKIE_FILE}")
        else:
            print("\n⏰ 超时，请重新运行 python3 xhs_login.py")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
