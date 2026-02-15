#!/usr/bin/env bash
set -euo pipefail

if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required but not installed." >&2
  exit 2
fi

if [[ -z "${BASE_URL:-}" || -z "${ADMIN_EMAIL:-}" || -z "${ADMIN_PASSWORD:-}" ]]; then
  cat <<'EOF' >&2
Usage:
  BASE_URL="https://your-service.onrender.com" \
  ADMIN_EMAIL="admin@example.com" \
  ADMIN_PASSWORD="your-admin-password" \
  ./scripts/post_deploy_smoke.sh

Optional:
  CURL_TIMEOUT_SECONDS=20
EOF
  exit 2
fi

TIMEOUT_SECONDS="${CURL_TIMEOUT_SECONDS:-20}"
TMP_DIR="$(mktemp -d)"
COOKIE_JAR="$TMP_DIR/cookies.txt"
BODY_FILE="$TMP_DIR/body.txt"
trap 'rm -rf "$TMP_DIR"' EXIT

HTTP_STATUS=""
HTTP_BODY=""

BASE_URL="${BASE_URL%/}"
NORMALIZED_EMAIL="$(printf '%s' "$ADMIN_EMAIL" | tr '[:upper:]' '[:lower:]' | sed 's/^ *//;s/ *$//')"

request() {
  local method="$1"
  local path="$2"
  local payload="${3:-}"
  local url="${BASE_URL}${path}"

  if [[ -n "$payload" ]]; then
    HTTP_STATUS="$(curl -sS \
      --connect-timeout "$TIMEOUT_SECONDS" \
      --max-time "$TIMEOUT_SECONDS" \
      -o "$BODY_FILE" \
      -w "%{http_code}" \
      -X "$method" \
      -H "Content-Type: application/json" \
      -b "$COOKIE_JAR" \
      -c "$COOKIE_JAR" \
      --data "$payload" \
      "$url")"
  else
    HTTP_STATUS="$(curl -sS \
      --connect-timeout "$TIMEOUT_SECONDS" \
      --max-time "$TIMEOUT_SECONDS" \
      -o "$BODY_FILE" \
      -w "%{http_code}" \
      -X "$method" \
      -b "$COOKIE_JAR" \
      -c "$COOKIE_JAR" \
      "$url")"
  fi

  HTTP_BODY="$(cat "$BODY_FILE")"
}

expect_status() {
  local expected="$1"
  local label="$2"
  if [[ "$HTTP_STATUS" != "$expected" ]]; then
    echo "[FAIL] $label (expected $expected, got $HTTP_STATUS)" >&2
    if [[ -n "$HTTP_BODY" ]]; then
      echo "$HTTP_BODY" >&2
    fi
    exit 1
  fi
  echo "[PASS] $label"
}

request "GET" "/health"
expect_status "200" "GET /health returns 200"
if ! printf '%s' "$HTTP_BODY" | grep -Eq '"ok"[[:space:]]*:[[:space:]]*true'; then
  echo "[FAIL] GET /health body missing ok=true" >&2
  echo "$HTTP_BODY" >&2
  exit 1
fi
echo "[PASS] GET /health body includes ok=true"

request "GET" "/auth/me"
expect_status "401" "GET /auth/me returns 401 when logged out"

request "POST" "/auth/login" "{\"email\":\"${ADMIN_EMAIL}\",\"password\":\"${ADMIN_PASSWORD}\"}"
expect_status "200" "POST /auth/login returns 200"

request "GET" "/auth/me"
expect_status "200" "GET /auth/me returns 200 after login"

COMPACT_BODY="$(printf '%s' "$HTTP_BODY" | tr -d '[:space:]')"
if [[ "$COMPACT_BODY" != *"\"email\":\"${NORMALIZED_EMAIL}\""* ]]; then
  echo "[FAIL] /auth/me response email does not match expected admin user" >&2
  echo "$HTTP_BODY" >&2
  exit 1
fi
echo "[PASS] /auth/me response includes expected admin email"

echo "Smoke checks completed successfully."
