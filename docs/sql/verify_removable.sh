#!/usr/bin/env bash
# §9.8 可摘除性验收（收官终验）：**全程 curl / HTTP 跑通完整链路，零浏览器、零 web 进程**。
#
# 五步对应架构 §9.8 原文：建会话 → 后台订阅 SSE → 发查询 → 拉图层 GeoJSON → feedback 回传。
# 每步都把实测值打出来并断言（断言不过就非 0 退出，不给「看起来过了」留空间）。
# JSON 判定一律交给解释器：grep 字符串会被转义、嵌套与数字类型骗过去。
#
# 用法：
#   bash docs/sql/verify_removable.sh                              # 默认 127.0.0.1:8000
#   BASE=http://127.0.0.1:8010 PY=D:/python_env/geomind/Scripts/python.exe bash docs/sql/verify_removable.sh
#
# 前提只有宿主机后端（uvicorn）+ seekdb；`web/` 目录整个删掉也应全绿。
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
PY="${PY:-D:/python_env/geomind/Scripts/python.exe}"
QUERY="${QUERY:-帮我在春熙路附近选个咖啡馆位置}"
SSE_FILE="$(mktemp)"
BODY=""
SID=""

cleanup() { rm -f "$SSE_FILE"; }
trap cleanup EXIT

step() { printf '\n=== %s ===\n' "$1"; }

step "0. 探针：后端在 $BASE（本脚本不启 web）"
HEALTH="$(curl -sf "$BASE/healthz" --max-time 15)" || {
  echo "后端不通：$BASE（先起 uvicorn，见 README「起服务」）" >&2; exit 1; }
printf '%s' "$HEALTH" | "$PY" -c "
import json, sys
d = json.load(sys.stdin)
print('healthz =', d)
assert d['ok'] is True, '数据库不通'
assert d['skills'] >= 9, 'Skill 注册表没就绪'
"

step "1. POST /api/sessions → session_id"
BODY="$(curl -sf -X POST "$BASE/api/sessions" -H 'Content-Type: application/json' \
  -d '{"scene":"site_selection"}' --max-time 20)"
printf '%s' "$BODY" | "$PY" -c "
import json, sys
d = json.load(sys.stdin)
sid = d.get('session_id', '')
assert len(sid) >= 8, f'session_id 缺失：{d}'
print('session_id =', sid, '| version =', d.get('version'))
"
SID="$(printf '%s' "$BODY" | "$PY" -c "import json,sys; print(json.load(sys.stdin)['session_id'])")"

step "2. 后台订阅 SSE（与浏览器 EventSource 等价：curl -N）"
curl -sN "$BASE/api/sessions/$SID/events" --max-time 120 > "$SSE_FILE" 2>/dev/null &
SSE_PID=$!
sleep 2
echo "SSE 已连上（pid=$SSE_PID，当前 $(wc -c < "$SSE_FILE") 字节）"

step "3. POST /chat → 候选非空 + Map Spec 含 poi 图层"
BODY="$(curl -sf -X POST "$BASE/api/sessions/$SID/chat" -H 'Content-Type: application/json' \
  -d "{\"query\":\"$QUERY\"}" --max-time 120)"
printf '%s' "$BODY" | "$PY" -c "
import json, sys
d = json.load(sys.stdin)
assert d.get('interrupted') is False, f'停在 HITL：{d.get(\"question\")}'
cands = d.get('candidates') or []
print('候选数 =', len(cands), '| 首条 =',
      (cands[0].get('name'), str(cands[0].get('rating'))) if cands else None)
assert len(cands) > 0, '候选为空'
kinds = [ly.get('kind') for ly in (d.get('map_spec') or {}).get('layers', [])]
print('图层 kinds =', kinds)
assert 'poi' in kinds, 'Map Spec 没有 poi 图层'
print('decision.summary =', ((d.get('decision') or {}).get('summary') or '')[:56], '…')
"

step "4. GET /layers/l_candidates → FeatureCollection（出口 GCJ-02）"
curl -sf "$BASE/api/sessions/$SID/layers/l_candidates" --max-time 30 | "$PY" -c "
import json, sys
d = json.load(sys.stdin)
feats = d.get('features') or []
print('type =', d.get('type'), '| features =', len(feats))
assert d.get('type') == 'FeatureCollection', '不是 FeatureCollection'
assert len(feats) > 0, '图层几何为空'
lon, lat = feats[0]['geometry']['coordinates'][:2]
print('首点 =', round(lon, 5), round(lat, 5), '（成都主城量级：经 102~106 / 纬 29~32）')
assert 102 < lon < 106 and 29 < lat < 32, '图层坐标不像成都（坐标系或数据串了）'
"

step "5a. POST /feedback constraint_edit → accepted（卡 6 闭环）"
BODY="$(curl -sf -X POST "$BASE/api/sessions/$SID/feedback" -H 'Content-Type: application/json' \
  -d "{\"type\":\"constraint_edit\",\"payload\":{\"changes\":{\"radius_m\":3000},\"query\":\"$QUERY\"}}" \
  --max-time 120)"
printf '%s' "$BODY" | "$PY" -c "
import json, sys
d = json.load(sys.stdin)
res = d.get('result') or {}
print('accepted =', d.get('accepted'), '| changes =', d.get('changes'),
      '| 重算候选数 =', len(res.get('candidates') or []))
assert d.get('accepted') is True, 'constraint_edit 未被接受'
assert (d.get('changes') or {}).get('radius_m') == 3000, '变更没回显'
assert len(res.get('candidates') or []) > 0, '重算后候选为空'
"

step "5b. 陌生键 rent（§9.8 原文示例）→ 必须 422，被白名单拦下"
CODE="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/api/sessions/$SID/feedback" \
  -H 'Content-Type: application/json' \
  -d "{\"type\":\"constraint_edit\",\"payload\":{\"changes\":{\"rent\":80000},\"query\":\"$QUERY\"}}" \
  --max-time 30)"
echo "HTTP $CODE"
[ "$CODE" = "422" ] || { echo "陌生键应 422，实得 $CODE" >&2; exit 1; }

step "6. SSE 里闭环事件真的到过（前端可摘除但事件契约成立）"
kill "$SSE_PID" 2>/dev/null || true
wait "$SSE_PID" 2>/dev/null || true
SSE_PATH="$SSE_FILE" "$PY" -c "
import json, os
types = []
for line in open(os.environ['SSE_PATH'], encoding='utf-8'):
    line = line.strip()
    if line.startswith('data: '):
        try:
            types.append(json.loads(line[6:]).get('type'))
        except Exception:
            pass
need = ['message.start', 'constraints.updated', 'tool.end', 'layer.update', 'message.end']
print('事件数 =', len(types), '| 前 14 =', ' → '.join(t or '?' for t in types[:14]), '…')
missing = [n for n in need if n not in types]
assert not missing, f'SSE 缺事件：{missing}'
"

printf '\n== §9.8 可摘除性终验：全绿（零浏览器、零 web 进程）==\n'
