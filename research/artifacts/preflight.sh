#!/usr/bin/env bash
# =============================================================================
# PROBE PRE-FLIGHT — run this in MINUTE ONE of the 9-hour clock.
# Every check maps to a verified fact in research/01-VERIFIED-FACTS.md.
# A failure here re-plans the day. Discovering any of these at H6 loses it.
#
#   ES_URL=https://... ES_AUTH="elastic:pass" ./preflight.sh
#   (or ES_AUTH_HEADER="ApiKey abc123")
# =============================================================================
set -uo pipefail
ES_URL="${ES_URL:-http://localhost:9200}"
if [ -n "${ES_AUTH_HEADER:-}" ]; then AUTH=(-H "Authorization: ${ES_AUTH_HEADER}")
else AUTH=(-u "${ES_AUTH:-elastic:changeme}"); fi
q(){ curl -sS "${AUTH[@]}" -H 'Content-Type: application/json' "$@"; }
esql(){ q -XPOST "$ES_URL/_query?format=txt" -d "{\"query\":$(python3 -c 'import json,sys;print(json.dumps(sys.argv[1]))' "$1")}"; }
PASS=0; FAIL=0
ok(){   printf '  \033[32m✔ PASS\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad(){  printf '  \033[31m✘ FAIL\033[0m %s\n     → %s\n' "$1" "$2"; FAIL=$((FAIL+1)); }
warn(){ printf '  \033[33m! WARN\033[0m %s\n     → %s\n' "$1" "$2"; }
hdr(){  printf '\n\033[1m%s\033[0m\n' "$1"; }

hdr "[1/7] LICENCE — CHANGE_POINT requires PLATINUM [F1]"
LIC=$(q "$ES_URL/_license" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("license",{}).get("type","unknown"))' 2>/dev/null || echo unreachable)
case "$LIC" in
  platinum|enterprise|trial) ok "licence = $LIC" ;;
  unreachable) bad "cannot reach $ES_URL" "check ES_URL / credentials before anything else" ;;
  *) bad "licence = $LIC — CHANGE_POINT WILL NOT RUN"
        "run:  curl -XPOST '$ES_URL/_license/start_trial?acknowledge=true' — DO THIS NOW" ;;
esac

hdr "[2/7] VERSION — CHANGE_POINT ... BY requires >= 9.5 [F1]"
VER=$(q "$ES_URL/" | python3 -c 'import json,sys;print(json.load(sys.stdin)["version"]["number"])' 2>/dev/null || echo 0.0.0)
python3 - "$VER" <<'PY'
import sys
v=tuple(int(x) for x in sys.argv[1].split("-")[0].split(".")[:3])
if v>=(9,5,0): print(f"  \033[32m✔ PASS\033[0m version {sys.argv[1]} — BY clause available")
elif v>=(9,2,0): print(f"  \033[33m! WARN\033[0m version {sys.argv[1]} — CHANGE_POINT works but NO 'BY'.\n     → detection must loop per service; budget +40 min at H3")
else: print(f"  \033[31m✘ FAIL\033[0m version {sys.argv[1]} — CHANGE_POINT unavailable (needs >= 9.2)")
PY

hdr "[3/7] DATA — are EDOT traces arriving?"
N=$(esql 'FROM traces-*.otel-* | STATS n = COUNT(*)' | tail -2 | tr -dc '0-9')
if [ -n "$N" ] && [ "$N" -gt 0 ] 2>/dev/null; then ok "$N spans in traces-*.otel-*"
else bad "no spans found in traces-*.otel-*" "fix ingest first — nothing else matters"; fi

hdr "[4/7] SPAN KIND — the silent zero-rows trap [F4]"
KINDS=$(esql 'FROM traces-*.otel-* | STATS n = COUNT(*) BY kind | SORT n DESC')
echo "$KINDS" | sed 's/^/     /'
if echo "$KINDS" | grep -q "Server"; then ok "kind = 'Server' — otel-native mapping, queries are correct as written"
elif echo "$KINDS" | grep -q "SPAN_KIND_SERVER"; then
  bad "kind = 'SPAN_KIND_SERVER' — collector is NOT in otel-native mapping mode" \
      "EVERY query needs rewriting AND 'duration' is MICROseconds not nanoseconds. Re-plan now."
elif echo "$KINDS" | grep -q "SERVER"; then
  bad "kind = 'SERVER' — ECS mapping mode; field is 'span.kind' not 'kind'" \
      "rewrite queries for ECS field names, or switch the exporter to mapping_mode: otel"
else warn "could not determine kind values" "inspect manually before writing any graph logic"; fi

hdr "[5/7] SERVICE NAMES — exact strings for ground truth [D0-11]"
esql 'FROM traces-*.otel-* | STATS spans = COUNT(*) BY resource.attributes.service.name | SORT spans DESC | LIMIT 25' | sed 's/^/     /'
echo "     ^ copy these EXACTLY into probe-ground-truth ('product-catalog', not 'productcatalog')"

hdr "[6/7] BUCKET COUNT — CHANGE_POINT needs >= 22 per series [F1]"
esql 'FROM traces-*.otel-* | WHERE @timestamp >= NOW() - 20 minutes AND kind == "Server"
 | EVAL svc = resource.attributes.service.name
 | STATS n = COUNT(*) BY ts = BUCKET(@timestamp, 10 seconds), svc
 | STATS buckets = COUNT(*) BY svc | SORT buckets ASC | LIMIT 25' | sed 's/^/     /'
echo "     ^ ANY service under 22 is SILENTLY SKIPPED. Widen window or raise load-generator rate."

hdr "[7/7] BEDROCK — max_new_tokens defaults to 64 and truncates [F7]"
R=$(q -XPOST "$ES_URL/_inference/completion/probe-bedrock-claude" \
     -d '{"input":"Count from one to twenty in words, comma separated."}' 2>/dev/null)
if echo "$R" | grep -q "resource_not_found\|No inference endpoint"; then
  warn "endpoint probe-bedrock-claude not created yet" "create it in the H0:45-H2 Lane B block"
elif echo "$R" | grep -qi "twenty"; then ok "Bedrock responds and is NOT truncating"
elif echo "$R" | grep -qi "on-demand throughput"; then
  bad "bare model ID rejected" "ap-south-1 needs a CROSS-REGION INFERENCE PROFILE id (global./apac. prefix) [F10]"
else warn "response may be truncated — check max_new_tokens >= 1024" "$(echo "$R" | head -c 200)"; fi

printf '\n\033[1m═══ %d passed, %d failed ═══\033[0m\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] && echo "GO — proceed to the H0:45 build blocks." \
                  || echo "NO-GO — fix the failures above before building anything."
