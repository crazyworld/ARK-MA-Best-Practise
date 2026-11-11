#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
BASE="${ARK_BASE_URL:-https://ark.cn-beijing.volces.com/api/v3}"
BASE="${BASE%/}"
[[ "$BASE" == */api/v3 ]] || BASE="$BASE/api/v3"
ARK_BETA_HEADER="X-Ark-Beta: agentic-2026-06-01"
: "${ARK_API_KEY:?please export ARK_API_KEY}"
command -v curl >/dev/null
command -v jq >/dev/null

api() {
  local method="$1" path="$2" body="${3:-}"
  if [[ -n "$body" ]]; then
    curl -sS --fail-with-body -X "$method" "$BASE$path" \
      -H "Authorization: Bearer $ARK_API_KEY" \
      -H "$ARK_BETA_HEADER" \
      -H "Content-Type: application/json" \
      -d "$body"
  else
    curl -sS --fail-with-body -X "$method" "$BASE$path" \
      -H "Authorization: Bearer $ARK_API_KEY" \
      -H "$ARK_BETA_HEADER"
  fi
}

find_id() {
  local collection="$1" name="$2"
  api GET "/$collection?page_size=200" |
    jq -r --arg name "$name" '(.data // .items // [])[] | select(.name == $name) | .id' |
    head -1
}

ENV_NAME="$(jq -r .name "$ROOT/environment.json")"
ENVIRONMENT_ID="${DIAGNOSE_ENVIRONMENT_ID:-}"
if [[ -z "$ENVIRONMENT_ID" ]]; then
  ENVIRONMENT_ID="$(find_id environments "$ENV_NAME")"
fi
if [[ -z "$ENVIRONMENT_ID" ]]; then
  ENVIRONMENT_ID="$(api POST /environments "$(jq -c . "$ROOT/environment.json")" | jq -er '.data.id // .id')"
else
  api POST "/environments/$ENVIRONMENT_ID" \
    "$(jq -c '{config: .config}' "$ROOT/environment.json")" >/dev/null
fi

AGENT_JSON="$ROOT/agents/diagnose.json"
AGENT_NAME="$(jq -r .name "$AGENT_JSON")"
MODEL_ID="${DIAGNOSE_MODEL_ID:-$(jq -er '.model.id' "$AGENT_JSON")}"
SYSTEM="$(jq -Rs . <"$ROOT/agents/diagnose.system.md")"
PAYLOAD="$(jq -c --arg model "$MODEL_ID" --argjson system "$SYSTEM" \
  '.model.id = $model | .system = $system | del(.system_file)' "$AGENT_JSON")"
AGENT_ID="${DIAGNOSE_AGENT_ID:-}"
if [[ -z "$AGENT_ID" ]]; then
  AGENT_ID="$(find_id agents "$AGENT_NAME")"
fi
if [[ -n "$AGENT_ID" ]]; then
  VERSION="$(api GET "/agents/$AGENT_ID" | jq -er '.data.version // .version')"
  api POST "/agents/$AGENT_ID" "$(jq -c --argjson version "$VERSION" '. + {version: $version}' <<<"$PAYLOAD")" >/dev/null
else
  AGENT_ID="$(api POST /agents "$PAYLOAD" | jq -er '.data.id // .id')"
fi

# 把 ID 打印出来,由使用者追加到 ~/.arkagent/cases/diagnose/config.env(脚本不改任何配置文件)。
printf 'DIAGNOSE_ENVIRONMENT_ID=%s\nDIAGNOSE_AGENT_ID=%s\n' "$ENVIRONMENT_ID" "$AGENT_ID"
