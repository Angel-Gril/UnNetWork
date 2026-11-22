#!/usr/bin/env bash
# ============================================================
#  ur-refresh-run.sh —— URnetwork 刷新包装（扫描模式）
#
#  扫描现有槽位 → probe 验证 → 套模板 → 写缓存
#  不签发新节点、不删除槽位，节点不会自杀。
#
#  模式 A（默认）：生成 runtime + 写 cache（独立运行）
#  模式 B（SKIP_CACHE_WRITE=1）：只生成 runtime，
#         由 Node 侧 prepareSubscription 接管写 cache
# ============================================================
set -uo pipefail

APP=/opt/ur-sub
CACHE=/var/lib/private-subscription/cache
RUNTIME=$APP/runtime
KEYS=$APP/keys.txt
LOG=$APP/logs/scan.log
WEB=/var/www/ur

PARALLEL="${PARALLEL:-8}"
PROTOCOL="${PROTOCOL:-https}"
MAX_PROBE_MS="${MAX_PROBE_MS:-25000}"
COUNTRIES="${COUNTRIES:-}"

mkdir -p "$RUNTIME" "$APP/logs"
touch "$LOG"

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

log "=== 扫描开始 (parallel=$PARALLEL, proto=$PROTOCOL, max_probe=${MAX_PROBE_MS}ms) ==="

ARGS=(--keys "$KEYS" --out "$RUNTIME"
      --protocol "$PROTOCOL" --parallel "$PARALLEL"
      --max-probe-ms "$MAX_PROBE_MS")
[ -n "$COUNTRIES" ] && ARGS+=(--only "$COUNTRIES")

if ! python3 -u "$APP/scripts/ur_scan.py" "${ARGS[@]}" >>"$LOG" 2>&1; then
  log "!! ur_scan.py 退出码非 0"
  exit 1
fi

SRC="$RUNTIME/mihomo.yaml"
if [ ! -s "$SRC" ]; then
  log "!! 没有生成 mihomo.yaml"
  exit 1
fi

# 只统计 proxies: 段里的节点
COUNT=$(awk '/^proxies:/{f=1;next} /^proxy-groups:/{f=0} f && /^  - name:/{c++} END{print c+0}' "$SRC" 2>/dev/null || echo 0)
RES=$(python3 -c "
import json
try:
    d = json.load(open('$RUNTIME/inventory.json'))
    print(sum(1 for n in d if n.get('residential')))
except Exception:
    print(0)
" 2>/dev/null || echo 0)
log "生成 $COUNT 个节点（住宅 $RES）"

# ---- 复制到 web 目录 ----
if [ -d "$WEB" ]; then
  for f in mihomo.yaml nodes.txt subscription.txt report.md; do
    [ -f "$RUNTIME/$f" ] && cp -f "$RUNTIME/$f" "$WEB/" || true
  done
  chmod 644 "$WEB"/* 2>/dev/null || true
fi

# ---- 模式 B：交给 Node 写 cache ----
if [ "${SKIP_CACHE_WRITE:-0}" = "1" ]; then
  log "=== 完成（runtime only）: $COUNT 个节点 ==="
  exit 0
fi

# ---- 模式 A：自己写 cache ----
SHA=$(sha256sum "$SRC" | awk '{print $1}')
NOW=$(date -u +%Y-%m-%dT%H:%M:%S.000Z)

[ -f "$CACHE/urnetwork.yaml" ] && cp -f "$CACHE/urnetwork.yaml" "$CACHE/urnetwork.previous.yaml" 2>/dev/null || true
cp -f "$SRC" "$CACHE/urnetwork.yaml"
chown subsvc:subsvc "$CACHE/urnetwork.yaml" 2>/dev/null || true
chmod 640 "$CACHE/urnetwork.yaml" 2>/dev/null || true

cat > "$CACHE/urnetwork.json" <<EOF
{
  "schemaVersion": 1,
  "provider": "urnetwork",
  "updatedAt": "$NOW",
  "changed": true,
  "sha256": "$SHA",
  "proxyCount": $COUNT,
  "validatedHostCount": 1,
  "recoveredServerCount": 0,
  "delegatedHostCount": 0,
  "lastError": null,
  "residentialCount": $RES,
  "authentication": { "mode": "api_key", "sessionRenewed": false, "accountLogin": false },
  "routing": { "template": "flybird", "geoUpdateIntervalHours": 24 }
}
EOF
chown subsvc:subsvc "$CACHE/urnetwork.json" 2>/dev/null || true
chmod 600 "$CACHE/urnetwork.json" 2>/dev/null || true

log "=== 完成: $COUNT 个节点（住宅 $RES），sha256=${SHA:0:16}… ==="
