# Xianyu auto sync - saves to shared folder
# Run from Windows host
$PROJECT = "D:\AI_PROJ\searchWebInfo"
$OUTPUT = "$PROJECT\data\xianyu_import.json"

Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Scraping Xianyu..." -ForegroundColor Cyan
try {
    $env:PYTHONIOENCODING = "utf-8"
    $result = & python "$PROJECT/src/crawler/xianyu_scraper.py" --search "杭州租房" --full --json --limit 15
    if ($LASTEXITCODE -ne 0 -or -not $result) {
        Write-Host "  FAIL: scraper error or empty" -ForegroundColor Red
        exit 1
    }
    $result | Out-File $OUTPUT -Encoding UTF8
    Write-Host "  OK: saved to data/xianyu_import.json" -ForegroundColor Green
} catch {
    Write-Host "  FAIL: $_" -ForegroundColor Red
    exit 1
}
