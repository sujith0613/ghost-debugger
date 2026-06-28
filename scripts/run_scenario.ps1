#!/usr/bin/env pwsh
# scripts/run_scenario.ps1
# Run a complete failure scenario against live instrumented services.
# Usage: .\scripts\run_scenario.ps1 [-Scenario cascade|resource|traffic|all]

param([string]$Scenario = "cascade")

$TIMESTAMP = (Get-Date -Format "yyyyMMddTHHmmssZ")
$OUTPUT_DIR = "docs/postmortem-examples"
$null = New-Item -ItemType Directory -Path $OUTPUT_DIR -Force

$AGENT_URL = "http://localhost:8090"
$INJECTOR_URL = "http://localhost:8099"
$SERVICE_A = "http://localhost:8081"

function Log   { Write-Host "[$(Get-Date -Format HH:mm:ss)] $args" -ForegroundColor Blue }
function Ok    { Write-Host "  [OK] $args" -ForegroundColor Green }
function Warn  { Write-Host "  [WARN] $args" -ForegroundColor Yellow }
function Fail  { Write-Host "  [FAIL] $args" -ForegroundColor Red }

function Generate-Traffic {
    param($Count, $Interval = 0.3, $Label = "baseline")
    Log "Generating $Count requests ($Label)"
    $ok = 0; $err = 0
    1..$Count | ForEach-Object {
        try {
            $s = (Invoke-WebRequest -Uri "$SERVICE_A/api/process" -TimeoutSec 15 -UseBasicParsing -ErrorAction Stop).StatusCode
            if ($s -eq 200) { $ok++ } else { $err++ }
        } catch { $err++ }
        Start-Sleep -Milliseconds ($Interval * 1000)
    }
    Ok "Traffic complete: $ok OK, $err errors"
}

function Wait-ForAnalysis {
    param($IncidentId, $MaxWait = 180)
    Log "Waiting for analysis to complete (max ${MaxWait}s)..."
    $elapsed = 0
    while ($elapsed -lt $MaxWait) {
        try {
            $detail = Invoke-RestMethod -Uri "$AGENT_URL/incidents/$IncidentId" -TimeoutSec 5 -ErrorAction Stop
            if ($detail.status -eq "complete") { Ok "Analysis complete after ${elapsed}s"; return $true }
            if ($detail.status -eq "failed")   { Fail "Analysis failed after ${elapsed}s"; return $false }
        } catch {}
        Start-Sleep -Seconds 5
        $elapsed += 5
    }
    Fail "Analysis timed out after ${MaxWait}s"
    return $false
}

function Save-Results {
    param($IncidentId, $ScenarioName)
    Log "Saving results for $IncidentId"
    $detail = Invoke-RestMethod -Uri "$AGENT_URL/incidents/$IncidentId" -TimeoutSec 10
    $detail | ConvertTo-Json -Depth 10 | Set-Content "$OUTPUT_DIR/${ScenarioName}_state.json"
    if ($detail.postmortem_report) {
        $detail.postmortem_report | Set-Content "$OUTPUT_DIR/${ScenarioName}_postmortem.md"
        Ok "Postmortem saved: $($detail.postmortem_report.Length) chars"
    }
    Write-Host "  Severity:    $($detail.triage_severity)"
    Write-Host "  Root Cause:  $($detail.root_cause)"
    Write-Host "  Confidence:  $([math]::Round($detail.root_cause_confidence * 100, 0))%"
    Write-Host "  Completed:   $($detail.completed_agents | Select-Object -Unique)"
    Write-Host "  Failed:      $($detail.failed_agents | Select-Object -Unique)"
    Ok "Results saved to $OUTPUT_DIR/"
}

function Reset-Failures {
    Invoke-RestMethod -Uri "$INJECTOR_URL/reset" -Method Post -UseBasicParsing -ErrorAction SilentlyContinue | Out-Null
    Ok "All failures reset"
}

function Trigger-Analysis {
    param($Description, $Services, $TriggerType = "error_rate")
    $body = @{
        trigger_type = $TriggerType
        trigger_description = $Description
        affected_services = $Services
        analysis_window_seconds = 600
    } | ConvertTo-Json
    $result = Invoke-RestMethod -Uri "$AGENT_URL/analyze" -Method Post -Body $body -ContentType "application/json"
    return $result.incident_id
}

function Run-Cascade {
    Write-Host "`n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
    Write-Host "  SCENARIO 1: CASCADE FAILURE" -ForegroundColor Cyan
    Write-Host "  service_b latency spike -> service_a timeout -> cascade" -ForegroundColor Cyan
    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
    Generate-Traffic -Count 20 -Interval 0.5 -Label "baseline"
    Reset-Failures
    Log "Injecting 2500ms latency into service_b"
    Invoke-RestMethod -Uri "$INJECTOR_URL/inject" -Method Post -Body (@{service="service_b";type="latency";duration_seconds=180;duration_ms=2500} | ConvertTo-Json) -ContentType "application/json" -UseBasicParsing | Out-Null
    Ok "Failure injected"
    Generate-Traffic -Count 30 -Interval 1.0 -Label "during-failure"
    $id = Trigger-Analysis -Description "error_rate and latency spike detected - service_b p99 latency 2500ms causing service_a timeouts" -Services @("service_a","service_b") -TriggerType "latency_p99"
    Ok "Incident: $id"
    $ok = Wait-ForAnalysis -IncidentId $id -MaxWait 180
    Save-Results -IncidentId $id -ScenarioName "scenario1_cascade_${TIMESTAMP}"
    Reset-Failures
    return $ok
}

function Run-Resource {
    Write-Host "`n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
    Write-Host "  SCENARIO 2: RESOURCE EXHAUSTION" -ForegroundColor Cyan
    Write-Host "  service_b error rate spike - simulated DB pool exhaustion" -ForegroundColor Cyan
    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
    Generate-Traffic -Count 20 -Interval 0.5 -Label "baseline"
    Reset-Failures
    Log "Injecting 40% error rate into service_b"
    Invoke-RestMethod -Uri "$INJECTOR_URL/inject" -Method Post -Body (@{service="service_b";type="error_rate";duration_seconds=180;error_rate=0.40} | ConvertTo-Json) -ContentType "application/json" -UseBasicParsing | Out-Null
    Ok "Failure injected"
    Generate-Traffic -Count 40 -Interval 0.8 -Label "during-failure"
    $id = Trigger-Analysis -Description "error_rate 40.2% exceeded 5% threshold on service_b - multiple ResourceExhausted errors in logs" -Services @("service_a","service_b") -TriggerType "error_rate"
    Ok "Incident: $id"
    $ok = Wait-ForAnalysis -IncidentId $id -MaxWait 180
    Save-Results -IncidentId $id -ScenarioName "scenario2_resource_${TIMESTAMP}"
    Reset-Failures
    return $ok
}

function Run-Traffic {
    Write-Host "`n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
    Write-Host "  SCENARIO 3: TRAFFIC SPIKE" -ForegroundColor Cyan
    Write-Host "  10x normal traffic -> downstream services degrade" -ForegroundColor Cyan
    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
    Generate-Traffic -Count 15 -Interval 0.5 -Label "baseline"
    Reset-Failures
    Log "Sending 100 rapid requests to service_a"
    $ok=0; $err=0
    1..100 | ForEach-Object {
        try { $s=(Invoke-WebRequest -Uri "$SERVICE_A/api/process" -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop).StatusCode; if($s-eq200){$ok++}else{$err++} } catch {$err++}
    }
    Ok "Spike sent: $ok OK, $err errors"
    Generate-Traffic -Count 60 -Interval 1.0 -Label "elevated"
    $id = Trigger-Analysis -Description "request_rate spike detected - service_a receiving 10x normal traffic, downstream error rate elevated" -Services @("service_a","service_b","service_c") -TriggerType "error_rate"
    Ok "Incident: $id"
    $ok = Wait-ForAnalysis -IncidentId $id -MaxWait 180
    Save-Results -IncidentId $id -ScenarioName "scenario3_traffic_${TIMESTAMP}"
    return $ok
}

switch ($Scenario) {
    "cascade"  { Run-Cascade }
    "resource" { Run-Resource }
    "traffic"  { Run-Traffic }
    "all"      { Run-Cascade; Start-Sleep 10; Run-Resource; Start-Sleep 10; Run-Traffic; Write-Host "`nAll scenarios complete." -ForegroundColor Cyan }
    default    { Write-Host "Usage: $PSCommandPath [-Scenario cascade|resource|traffic|all]" }
}
