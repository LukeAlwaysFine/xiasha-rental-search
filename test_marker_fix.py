import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox','--disable-dev-shm-usage','--disable-gpu'])
        ctx = await browser.new_context(viewport={"width":1280,"height":800})
        page = await ctx.new_page()
        
        await page.goto('http://127.0.0.1:8000/', timeout=15000)
        await page.wait_for_timeout(3000)
        await page.fill('#keyword', '下沙')
        await page.press('#keyword', 'Enter')
        await page.wait_for_timeout(20000)
        
        result = await page.evaluate("""
(function() {
    var cards = document.querySelectorAll('.listing-card');
    var cardIds = new Set();
    cards.forEach(function(c) {
        var m = c.id.match(/^card-(\\d+)$/);
        if (m) cardIds.add(parseInt(m[1]));
    });
    
    var markerMapCount = Object.keys(markerMap).length;
    var allValid = true;
    for (var k in markerMap) {
        if (!document.getElementById('card-' + k)) { allValid = false; break; }
    }
    
    var cardsWithMarkers = 0;
    cards.forEach(function(c) {
        var m = c.id.match(/^card-(\\d+)$/);
        if (m && markerMap[m[1]]) cardsWithMarkers++;
    });
    
    return {
        cards: cards.length,
        markers: markers.length,
        markerMapSize: markerMapCount,
        allValid: allValid,
        cardsWithMarkers: cardsWithMarkers,
        coverage: cards.length > 0 ? Math.round(cardsWithMarkers / cards.length * 100) : 0
    };
})()
""")
        print(result)
        
        if result['allValid']:
            print("PASS: all markerMap entries have cards")
        if result['coverage'] >= 70:
            print(f"PASS: {result['coverage']}% card coverage")
        
        await browser.close()

asyncio.run(main())
