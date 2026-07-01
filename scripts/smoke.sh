#!/usr/bin/env sh
set -eu

BASE_URL="${BASE_URL:-http://localhost:11010}"
OUT_DIR="${OUT_DIR:-./tmp/smoke}"
APKPURE_XAPK_PACKAGE="${APKPURE_XAPK_PACKAGE:-}"
# 云上（AUTH_ENABLED=true）时设 API_KEY=<管理员在 /admin 创建的 Key>，数据 API 自动带 Authorization: Bearer。
# 内网（无鉴权）时留空，行为与原来一致。
API_KEY="${API_KEY:-}"

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"

# 数据 API 用 auth_curl：设了 API_KEY 就带 Bearer 头（公开端点收到也无害）。
auth_curl() {
  if [ -n "$API_KEY" ]; then
    curl -H "Authorization: Bearer $API_KEY" "$@"
  else
    curl "$@"
  fi
}

curl_json() {
  path="$1"
  auth_curl -fsS "$BASE_URL$path" -H "X-Request-ID: smoke-$(date +%s)"
}

download() {
  path="$1"
  name="$2"
  response="$OUT_DIR/$name.response"
  status_code="$(auth_curl -sS -L "$BASE_URL$path" -H "X-Request-ID: smoke-$name" -o "$response" -w "%{http_code}")"
  if [ "$status_code" = "200" ]; then
    mv "$response" "$OUT_DIR/$name"
    return
  fi
  if [ "$status_code" != "202" ]; then
    echo "download $path failed with HTTP $status_code" >&2
    cat "$response" >&2
    return 1
  fi

  status_url="$(json_get "$response" statusUrl)"
  i=0
  while [ "$i" -lt 120 ]; do
    auth_curl -fsS "$status_url" -H "X-Request-ID: smoke-$name-status" -o "$response"
    job_status="$(json_get "$response" status)"
    if [ "$job_status" = "succeeded" ]; then
      file_url="$(json_get "$response" fileUrl)"
      auth_curl -fsSL "$file_url" -H "X-Request-ID: smoke-$name-file" -o "$OUT_DIR/$name"
      return
    fi
    if [ "$job_status" = "failed" ]; then
      echo "download job failed for $path" >&2
      cat "$response" >&2
      return 1
    fi
    i=$((i + 1))
    sleep 1
  done

  echo "download job timed out for $path" >&2
  cat "$response" >&2
  return 1
}

json_get() {
  file="$1"
  key="$2"
  python3 - "$file" "$key" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as f:
    value = json.load(f).get(sys.argv[2])
print("" if value is None else value)
PY
}

curl_json "/health" | grep -q '"status":"ok"'

# 云上鉴权自检（设了 API_KEY 即认为 AUTH_ENABLED=true）：健康检查公开、数据 API 无 Key 401、面板未登录 302。
if [ -n "$API_KEY" ]; then
  code="$(curl -sS -o /dev/null -w '%{http_code}' "$BASE_URL/api/v1/android/apps/org.fdroid.fdroid?provider=fake")"
  test "$code" = "401" || { echo "expected 401 without API key, got $code" >&2; exit 1; }
  code="$(curl -sS -o /dev/null -w '%{http_code}' "$BASE_URL/dashboard")"
  test "$code" = "302" || { echo "expected 302 for /dashboard without login, got $code" >&2; exit 1; }
  echo "auth smoke ok: 401 without key, 302 for /dashboard"
fi

curl_json "/api/v1/android/apps/org.fdroid.fdroid?provider=fake" | grep -q '"provider":"fake"'
curl_json "/api/v1/android/apps/org.fdroid.fdroid/files?provider=fake" | grep -q '"files"'

download "/api/v1/android/apps/org.fdroid.fdroid/download?provider=fake" "fdroid.apk"
test "$(dd if="$OUT_DIR/fdroid.apk" bs=2 count=1 2>/dev/null)" = "PK"

download "/api/v1/android/apps/com.oakever.arrows/download?provider=fake" "arrows.xapk"
unzip -l "$OUT_DIR/arrows.xapk" | grep -q "manifest.json"
unzip -l "$OUT_DIR/arrows.xapk" | grep -q "config.arm64_v8a.apk"

download "/api/v1/android/apps/org.fdroid.fdroid/download?provider=fake" "fdroid-reused.apk"
cmp "$OUT_DIR/fdroid.apk" "$OUT_DIR/fdroid-reused.apk"

curl_json "/api/v1/android/apps/org.fdroid.fdroid?provider=auto" | grep -q '"packageName":"org.fdroid.fdroid"'
auth_curl -sS "$BASE_URL/api/v1/android/apps/org.fdroid.fdroid?provider=fake-failing" \
  -H "X-Request-ID: smoke-failing-provider" | grep -q '"NETWORK_ERROR"'

if [ -n "$APKPURE_XAPK_PACKAGE" ]; then
  download "/api/v1/android/apps/$APKPURE_XAPK_PACKAGE/download?provider=apkpure-signed" "apkpure.xapk"
  unzip -t "$OUT_DIR/apkpure.xapk" >/dev/null
fi

echo "smoke ok: $BASE_URL"
