# Xianyu auto sync - writes to shared folder, container auto-imports
$PROJECT = "D:\AI_PROJ\searchWebInfo"
$OUT = "$PROJECT\data\xianyu.json"
$INTERVAL = 7200

function Sync {
    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Scraping..." -ForegroundColor Cyan
    $env:PYTHONIOENCODING = "utf-8"
    New-Item -Path "$PROJECT\data" -ItemType Directory -Force | Out-Null
    python "$PROJECT/src/crawler/xianyu_scraper.py" --search "杭州租房" --full --output "$OUT" --limit 30
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  OK: saved" -ForegroundColor Green
    } else {
        Write-Host "  FAIL" -ForegroundColor Red
    }
}

Write-Host "=== Xianyu Auto Sync ===" -ForegroundColor Yellow
Sync
while ($true) {
    $next = (Get-Date).AddSeconds($INTERVAL)
    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Next at $($next.ToString('HH:mm'))"
    Start-Sleep -Seconds $INTERVAL
    Sync
}
