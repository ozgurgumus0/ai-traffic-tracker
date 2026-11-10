# Automated refresh for the AI traffic tracker dashboard.
# Called by Windows Task Scheduler, or by the dashboard's REFRESH button via the
# acme-refresh:// protocol handler (HKCU\Software\Classes\acme-refresh):
#   -Mode ga4 : GA4 AI-referral pull (daily, free)
#   -Mode geo : direct model-presence run (weekly, costs API tokens)
#   -Mode all : GA4 + direct model-presence run (no Peec dependency)
# Both rewrite the JS sidecars the dashboard auto-loads — just open dashboard.html.
# Logs to logs\refresh.log (rolling, last 500 lines kept).

param([ValidateSet("ga4", "geo", "demand", "searchconsole", "aio", "adhoc", "analyst", "peec", "all")][string]$Mode = "ga4",
      [string]$FromProtocol = "",
      [string]$Question = "",
      [switch]$Publish)   # off by default: only the project folder is updated

# browser buttons send e.g. "acme-refresh://geo/", "acme-refresh://peec/",
# "acme-refresh://all/" or "acme-refresh://adhoc/?q=<urlencoded question>" as %1
if ($FromProtocol) {
    if ($FromProtocol -match "adhoc") {
        $Mode = "adhoc"
        if ($FromProtocol -match "q=([^&]+)") {
            $Question = [System.Uri]::UnescapeDataString($Matches[1])
        }
    } elseif ($FromProtocol -match "analyst") {
        $Mode = "analyst"
        if ($FromProtocol -match "q=([^&]+)") { $Question = [System.Uri]::UnescapeDataString($Matches[1]) }
    } elseif ($FromProtocol -match "all") { $Mode = "all" }
    elseif ($FromProtocol -match "peec") { $Mode = "peec" }
    elseif ($FromProtocol -match "geo") { $Mode = "geo" } else { $Mode = "ga4" }
}

$ErrorActionPreference = "Continue"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = if ($env:PYTHON_EXE) { $env:PYTHON_EXE } else { "python" }
$LogDir = Join-Path $Here "logs"
$Log = Join-Path $LogDir "refresh.log"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory $LogDir | Out-Null }
$env:PYTHONIOENCODING = "utf-8"

function Write-Log($msg) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg" | Out-File $Log -Append -Encoding utf8
}

Write-Log "================ refresh start (mode=$Mode) ================"
if ($Mode -eq "analyst") {
    if (-not $Question) { Write-Log "    analyst: empty question, aborting" }
    else {
        Write-Log "    analyst question received"
        & $Python (Join-Path $Here "analyst_chat.py") --question $Question 2>&1 | ForEach-Object { Write-Log "    $_" }
    }
} elseif ($Mode -eq "adhoc") {
    if (-not $Question) { Write-Log "    adhoc: empty question, aborting" }
    else {
        Write-Log "    question: $Question"
        & $Python (Join-Path $Here "geo_monitor.py") --ask $Question 2>&1 |
            ForEach-Object { Write-Log "    $_" }
    }
} elseif ($Mode -eq "all") {
    # Full refresh for the dashboard: verified GA4 outcomes + our own direct
    # model/API tests. Peec is deliberately excluded from this data path.
    foreach ($s in @("ga4_ai_traffic.py", "keyword_demand.py")) {
        Write-Log "    --- $s ---"
        & $Python (Join-Path $Here $s) 2>&1 | ForEach-Object { Write-Log "    $_" }
    }
    Write-Log "    --- geo_monitor.py (quick: 55 prompts, 1 sample, 8 workers) ---"
    & $Python (Join-Path $Here "geo_monitor.py") --runs 1 --workers 8 2>&1 | ForEach-Object { Write-Log "    $_" }
} else {
    if ($Mode -eq "geo") {
        Write-Log "    --- geo_monitor.py (quick: 55 prompts, 1 sample, 8 workers) ---"
        & $Python (Join-Path $Here "geo_monitor.py") --runs 1 --workers 8 2>&1 | ForEach-Object { Write-Log "    $_" }
    } else {
        $script = switch ($Mode) {
            "ga4"  { "ga4_ai_traffic.py" }
            "demand" { "keyword_demand.py" }
            "searchconsole" { "search_console_queries.py" }
            "aio" { "aio_monitor.py" }
            "peec" { "peec_pull.py" }
            default { "ga4_ai_traffic.py" }
        }
        & $Python (Join-Path $Here $script) 2>&1 | ForEach-Object { Write-Log "    $_" }
    }
    # weekly with the Peec pull: refresh the citation analysis (Where-to-win tab)
    # and the boss-facing one-pager
    if ($Mode -eq "peec") {
        # citation_analysis emits Peec's opportunity-scored recommendations (free,
        # no LLM). --llm (the verbose Claude prose) is intentionally NOT passed.
        & $Python (Join-Path $Here "citation_analysis.py") 2>&1 | ForEach-Object { Write-Log "    $_" }
        & $Python (Join-Path $Here "executive_summary.py") 2>&1 | ForEach-Object { Write-Log "    $_" }
    }
}
if ($LASTEXITCODE -ne 0) { Write-Log "    EXIT CODE $LASTEXITCODE (failed)" }

# Optional: publish a viewer copy to the shared "AI Monitor"
# folder. OFF by default — the working folder (this project) is the only thing
# the dashboard reads. Pass -Publish to also sync the shared copy.
if ($Publish) {
    $ShareRoot = $env:SHARE_DIR
    $Share = Join-Path $ShareRoot "AI Monitor"
    if (-not $ShareRoot -or -not (Test-Path $ShareRoot)) { $Share = $null }
    if ($Share -and -not (Test-Path $Share)) { New-Item -ItemType Directory $Share | Out-Null }
    foreach ($f in @("dashboard.html", "ai_traffic_data.js", "geo_data.js", "keyword_demand_data.js", "search_console_data.js", "aio_data.js")) {
        if (-not $Share) { break }
        $src = Join-Path $Here $f
        if (Test-Path $src) {
            Copy-Item $src (Join-Path $Share $f) -Force
            Write-Log "published $f to the shared AI Monitor folder"
        }
    }
}

$lines = Get-Content $Log
if ($lines.Count -gt 500) { $lines | Select-Object -Last 500 | Set-Content $Log -Encoding utf8 }
Write-Log "================ refresh done (mode=$Mode) ================"
