#!/bin/bash
set -uo pipefail

input=$(cat)

get_json_field() {
  local key="$1"
  local jqpath="$2"
  if command -v jq >/dev/null 2>&1; then
    printf '%s' "$input" | jq -r "$jqpath // empty" 2>/dev/null
  else
    printf '%s' "$input" | grep -o "\"$key\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" | head -n1 | sed -E "s/.*\"$key\"[[:space:]]*:[[:space:]]*\"([^\"]*)\"/\1/"
  fi
}

tool_name=$(get_json_field "tool_name" ".tool_name")
file_path=$(get_json_field "file_path" ".tool_input.file_path")
command=$(get_json_field "command" ".tool_input.command")

deny() {
  local reason="$1"
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"%s"}}\n' "$reason"
  exit 2
}

lc() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

is_under_raw() {
  case "$1" in
    assets/raw/*|*/assets/raw/*) return 0 ;;
    *) return 1 ;;
  esac
}

is_cred_file() {
  local base
  base=$(basename -- "$(lc "$1")")
  case "$base" in
    .env|.env.*) return 0 ;;
    *credential*|*secret*) return 0 ;;
  esac
  return 1
}

case "$tool_name" in
  Write|Edit)
    if [ -n "$file_path" ] && is_under_raw "$file_path"; then
      deny "assets/raw/ holds immutable original merchant assets; writes/edits are blocked."
    fi
    if [ -n "$file_path" ] && is_cred_file "$file_path"; then
      deny "credential/secret files may not be read or edited by the agent."
    fi
    ;;
  Read)
    if [ -n "$file_path" ] && is_cred_file "$file_path"; then
      deny "credential/secret files may not be read or edited by the agent."
    fi
    ;;
  Bash)
    if [ -n "$command" ]; then
      lccmd=$(lc "$command")
      if printf '%s' "$lccmd" | grep -Eq 'assets/raw/'; then
        if printf '%s' "$lccmd" | grep -Eq '(^|[[:space:];&|])(rm|mv|truncate)([[:space:]]|$)|(^|[[:space:];&|])cp[[:space:]].*assets/raw/|>+[[:space:]]*[^|]*assets/raw/|sed[[:space:]]+.*-i'; then
          deny "assets/raw/ is immutable; mutating shell commands against it are blocked."
        fi
      fi
      if printf '%s' "$lccmd" | grep -Eq '\.env([^a-z0-9_.-]|$)|credential|secret'; then
        if printf '%s' "$lccmd" | grep -Eq '(^|[[:space:];&|])(cat|less|more|head|tail|grep|strings|xxd|od|hexdump)([[:space:]]|$)'; then
          deny "reading credential/secret files via shell is blocked."
        fi
      fi
      # Credentials live in this process's environment. The agent also ingests untrusted
      # marketplace text (scraped portal content, keyword strings), so a dump of the
      # environment is an exfiltration path. Block the dump commands outright.
      if printf '%s' "$lccmd" | grep -Eq '(^|[[:space:];&|`(])(printenv|export[[:space:]]+-p|declare[[:space:]]+-x)([[:space:]]|$|;|\|)'; then
        deny "dumping the environment is blocked; it holds live marketplace API credentials."
      fi
      # bare `env` (a dump) but not the `env VAR=val cmd` prefix form, which is legitimate.
      if printf '%s' "$lccmd" | grep -Eq '(^|[[:space:];&|`(])env([[:space:]]*($|;|\||&))'; then
        deny "dumping the environment is blocked; it holds live marketplace API credentials."
      fi
      # never expand a known credential variable into output
      if printf '%s' "$command" | grep -Eq '\$\{?(AMAZON_LWA_[A-Z_]+|AMAZON_ADS_[A-Z_]+|FLIPKART_APP_[A-Z_]+|[A-Z_]*(SECRET|REFRESH_TOKEN|ACCESS_TOKEN|API_KEY|PASSWORD)[A-Z_]*)\}?'; then
        deny "expanding a credential environment variable is blocked. Check presence with a test like [ -n \"\${VAR:-}\" ] && echo set, never print the value."
      fi
      # the minted-token cache is bearer credentials on disk
      if printf '%s' "$lccmd" | grep -Eq '\.mp-cache'; then
        if printf '%s' "$lccmd" | grep -Eq '(^|[[:space:];&|])(cat|less|more|head|tail|grep|strings|xxd|od|hexdump)([[:space:]]|$)'; then
          deny "the .mp-cache token cache holds live access tokens; reading it is blocked."
        fi
      fi
    fi
    ;;
esac

exit 0
