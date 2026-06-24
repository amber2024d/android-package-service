#!/usr/bin/env sh
set -eu

BASE_URL="${BASE_URL:-http://localhost:11010}"
OUT_DIR="${OUT_DIR:-./tmp/smoke}"
APKPURE_XAPK_PACKAGE="${APKPURE_XAPK_PACKAGE:-}"

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"

curl_json() {
  path="$1"
  curl -fsS "$BASE_URL$path" -H "X-Request-ID: smoke-$(date +%s)"
}

download() {
  path="$1"
  name="$2"
  curl -fsSL "$BASE_URL$path" -H "X-Request-ID: smoke-$name" -o "$OUT_DIR/$name"
}

curl_json "/health" | grep -q '"status":"ok"'
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
curl -sS "$BASE_URL/api/v1/android/apps/org.fdroid.fdroid?provider=fake-failing" \
  -H "X-Request-ID: smoke-failing-provider" | grep -q '"NETWORK_ERROR"'

if [ -n "$APKPURE_XAPK_PACKAGE" ]; then
  download "/api/v1/android/apps/$APKPURE_XAPK_PACKAGE/download?provider=apkpure-signed" "apkpure.xapk"
  unzip -t "$OUT_DIR/apkpure.xapk" >/dev/null
fi

echo "smoke ok: $BASE_URL"
