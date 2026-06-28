#!/usr/bin/env pwsh
# scripts/preflight_check.ps1
param([switch]$Quiet)

$PASS = 0; $FAIL = 0; $WARN = 0

function Check-Http {
    param($Name, $Url, $Expected = 200)
    try {
        $status = (Invoke-WebRequest -Uri $Url -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop).StatusCode
        if ($status -eq $Expected) {
            if (-not $Quiet) { Write-Host "  [PASS] $Name" -ForegroundColor Green }
            $script:PASS++
        } else {
            if (-not $Quiet) { Write-Host "  [FAIL] $Name - HTTP $status (expected $expected)" -ForegroundColor Red }
            $script:FAIL++
        }
    } catch {
        if (-not $Quiet) { Write-Host "  [FAIL] $Name - $($_.Exception.Message)" -ForegroundColor Red }
        $script:FAIL++
    }
}

function Check-MetricExists {
    param($Name, $Metric)
    try {
        $r = Invoke-RestMethod -Uri "http://localhost:9090/api/v1/query?query=$Metric" -TimeoutSec 5 -ErrorAction Stop
        if ($r.data.result.Count -gt 0) {
            if (-not $Quiet) { Write-Host "  [PASS] $Name metric present" -ForegroundColor Green }
            $script:PASS++
        } else {
            if (-not $Quiet) { Write-Host "  [WARN] $Name metric empty" -ForegroundColor Yellow }
            $script:WARN++
        }
    } catch {
        if (-not $Quiet) { Write-Host "  [WARN] $Name metric unreachable" -ForegroundColor Yellow }
        $script:WARN++
    }
}

Write-Host "`n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
Write-Host "  Ghost Debugger - Pre-Flight Check" -ForegroundColor Cyan
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan

Write-Host "`n-- Infrastructure -------------------------------------" -ForegroundColor Blue
Check-Http "Jaeger UI"    "http://localhost:16686"
Check-Http "Prometheus"   "http://localhost:9090/-/healthy"
Check-Http "Grafana"      "http://localhost:3001/api/health"
Check-Http "ChromaDB"     "http://localhost:8000/api/v1/heartbeat"

Write-Host "`n-- Test Services --------------------------------------" -ForegroundColor Blue
Check-Http "service_a /health"         "http://localhost:8081/health"
Check-Http "service_b /health"         "http://localhost:8082/health"
Check-Http "service_c /health"         "http://localhost:8083/health"
Check-Http "failure_injector /health"  "http://localhost:8099/health"

Write-Host "`n-- Ghost Debugger -------------------------------------" -ForegroundColor Blue
Check-Http "Gateway HTTP"   "http://localhost:8080/health"
Check-Http "Agent Service"  "http://localhost:8090/health"
Check-Http "Dashboard"      "http://localhost:8090/"

Write-Host "`n-- API Key --------------------------------------------" -ForegroundColor Blue
if ($env:GOOGLE_API_KEY) {
    Write-Host "  [PASS] GOOGLE_API_KEY is set" -ForegroundColor Green
    $script:PASS++
} else {
    Write-Host "  [FAIL] GOOGLE_API_KEY is not set - agents will fail" -ForegroundColor Red
    $script:FAIL++
}

Write-Host "`n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
Write-Host "  $PASS passed  |  $WARN warnings  |  $FAIL failed" -ForegroundColor $(if ($FAIL -gt 0) { "Red" } elseif ($WARN -gt 0) { "Yellow" } else { "Green" })
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan

if ($FAIL -gt 0) {
    Write-Host "`nFix failing checks before running scenarios." -ForegroundColor Yellow
    exit 1
}
