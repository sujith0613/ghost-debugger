#!/bin/bash
# scripts/preflight_check.sh
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; NC='\033[0m'

PASS=0; FAIL=0; WARN=0

check_http() {
  local name=$1 url=$2 expected=${3:-200}
  local status
  status=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$url" 2>/dev/null || echo "000")
  if [ "$status" = "$expected" ]; then
    echo -e "  ${GREEN}✓${NC} $name"
    ((PASS++))
  else
    echo -e "  ${RED}✗${NC} $name — HTTP $status (expected $expected)"
    ((FAIL++))
  fi
}

check_metric_exists() {
  local name=$1 metric=$2
  local result
  result=$(curl -s "http://localhost:9090/api/v1/query?query=$metric" 2>/dev/null | python3 -c "
import json,sys
d=json.load(sys.stdin)
r=d.get('data',{}).get('result',[])
print('found' if r else 'empty')
" 2>/dev/null || echo "error")
  if [ "$result" = "found" ]; then
    echo -e "  ${GREEN}✓${NC} $name metric present"
    ((PASS++))
  else
    echo -e "  ${YELLOW}⚠${NC} $name metric empty (services may not have received traffic yet)"
    ((WARN++))
  fi
}

check_jaeger_services() {
  local services
  services=$(curl -s "http://localhost:16686/api/services" 2>/dev/null | python3 -c "
import json,sys
d=json.load(sys.stdin)
svcs=d.get('data',[])
print(','.join(svcs))
" 2>/dev/null || echo "")
  if echo "$services" | grep -q "service_a"; then
    echo -e "  ${GREEN}✓${NC} Jaeger has traces from test services: $services"
    ((PASS++))
  else
    echo -e "  ${YELLOW}⚠${NC} Jaeger has no traces yet — send some traffic first"
    ((WARN++))
  fi
}

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Ghost Debugger — Pre-Flight Check"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

echo "── Infrastructure ──────────────────────────────────────"
check_http "Jaeger UI"    "http://localhost:16686"             200
check_http "Prometheus"   "http://localhost:9090/-/healthy"    200
check_http "Grafana"      "http://localhost:3001/api/health"   200
check_http "ChromaDB"     "http://localhost:8000/api/v1/heartbeat" 200

echo ""
echo "── Test Services ───────────────────────────────────────"
check_http "service_a /health"         "http://localhost:8081/health" 200
check_http "service_b /health"         "http://localhost:8082/health" 200
check_http "service_c /health"         "http://localhost:8083/health" 200
check_http "failure_injector /health"  "http://localhost:8099/health" 200

echo ""
echo "── Ghost Debugger ──────────────────────────────────────"
check_http "Gateway HTTP"   "http://localhost:8080/health"  200
check_http "Agent Service"  "http://localhost:8090/health"  200
check_http "Dashboard"      "http://localhost:8090/"        200

echo ""
echo "── Observability ───────────────────────────────────────"
check_jaeger_services
check_metric_exists "HTTP request metrics" "http_requests_total"
check_metric_exists "Gateway ingestion"    "ghost_debugger_gateway_ingestion_total"

echo ""
echo "── API Key ─────────────────────────────────────────────"
if [ -n "${GOOGLE_API_KEY:-}" ]; then
  echo -e "  ${GREEN}✓${NC} GOOGLE_API_KEY is set"
  ((PASS++))
else
  echo -e "  ${RED}✗${NC} GOOGLE_API_KEY is not set — agents will fail"
  ((FAIL++))
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "  ${GREEN}$PASS passed${NC}  |  ${YELLOW}$WARN warnings${NC}  |  ${RED}$FAIL failed${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if [ $FAIL -gt 0 ]; then
  echo ""
  echo "Fix failing checks before running scenarios."
  echo "Start the full stack: docker compose --profile services --profile app up -d"
  exit 1
fi
echo ""
echo "System ready. Run: ./scripts/run_scenario.sh [cascade|resource|traffic]"
exit 0
