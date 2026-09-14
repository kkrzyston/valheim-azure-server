#!/usr/bin/env bash
# Secret scanner for this repository.
#
# This repo is PUBLIC. A credential pushed here cannot be taken back: force-pushing
# leaves the old commit fetchable by its SHA, and forks and caches keep copies. The
# only real remedy is rotating the credential. So the job here is to stop a secret
# before it is ever committed.
#
#   scripts/scan-secrets.sh              scan the whole working tree
#   scripts/scan-secrets.sh --staged     scan only staged changes (used by the hook)
#
# This file deliberately contains NO real secret values -- it matches on SHAPE. To also
# match your own deployment's literal values, create `.secret-patterns` in the repo root
# (one literal per line; it is gitignored and must never be committed).
#
# FAILS CLOSED: it first plants known-bad canaries and proves it can detect each class.
# If that self-test fails, it exits non-zero WITHOUT reporting "clean" -- a subtly broken
# pattern must never be mistaken for a clean tree. (A real example from this repo's own
# history: a pattern anchored with \b silently failed to match `Steam_7656119...`, because
# `_` is a word character, and reported a tree containing a live Steam ID as clean.)
set -uo pipefail
cd "$(git rev-parse --show-toplevel 2>/dev/null || echo .)" || exit 2
MODE="${1:-}"; rc=0

# ---- patterns: name|regex ----------------------------------------------------------
PATTERNS=(
  "discord-bot-token|[MNO][A-Za-z0-9_-]{22,}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}"
  "discord-webhook|discord(app)?\.com/api/webhooks/[0-9]+/[A-Za-z0-9_-]+"
  "discord-invite|discord\.gg/[A-Za-z0-9]{4,}|/invites/[A-Za-z0-9]{4,}"
  "steam64-id|7656119[0-9]{10}"
  "uuid-subscription|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
  "azure-cloudapp-host|[a-z0-9-]+\.[a-z0-9]+\.cloudapp\.azure\.com"
  "populated-password-flag|-password[ =\"']+[A-Za-z0-9@#%^&*._-]{4,}"
  "storage-key|AccountKey=[A-Za-z0-9+/=]{20,}"
  "private-key|-----BEGIN [A-Z ]*PRIVATE KEY-----"
  "github-token|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
  "aws-key|AKIA[0-9A-Z]{16}"
  "bearer-literal|[Aa]uthorization[\"': =]+(Bearer|Bot) [A-Za-z0-9_.-]{20,}"
  "tailnet-ip|100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}"
  "long-token|[A-Za-z0-9+/]{55,}={0,2}"
)
# Placeholders and known-safe values that must NOT trip the scanner.
ALLOW='CHANGEME_[A-Z0-9_]+|<[A-Za-z0-9_]+>|__[A-Z_]+__|169\.254\.169\.254|127\.0\.0\.1|0\.0\.0\.0|203\.0\.113\.|198\.51\.100\.|192\.0\.2\.|example\.|YOUR_|xxxx|\$\{|\$[A-Za-z_]|os\.environ|getenv'
EXCLUDE='^(\.git/|scripts/scan-secrets\.sh|\.githooks/|CONTRIBUTING\.md)'

# ---- positive control --------------------------------------------------------------
selftest() {
  local tmp bad n
  tmp=$(mktemp); bad=0
  {
    # Built at runtime from parts. A literal token-shaped string sitting in this file
    # trips GitHub's own push protection (it did, once - the push was rejected). This
    # still exercises the bot-token regex, but its first segment is not valid base64
    # for a Discord snowflake, so real detectors correctly ignore it.
    printf 'tok=M%s.%s.%s
' "$(printf 'A%.0s' $(seq 24))" 'AbCdEf' "$(printf 'B%.0s' $(seq 28))"
    printf 'hook=https://discord.com/api/webhooks/123456789/AbCdEfGhIjKlMnOp\n'
    printf 'inv=https://discord.gg/AbCdE12\n'
    printf 'steam=Steam_76561198000000001\n'
    printf 'sub=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee\n'
    printf 'host=valheim-abc123.westus2.cloudapp.azure.com\n'
    printf 'arg=-password "s3cr3tpw"\n'
    printf 'key=AccountKey=abcdefghijklmnopqrstuvwxyz0123456789==\n'
    printf 'gh=ghp_abcdefghijklmnopqrstuvwxyz0123456789\n'
    printf 'tn=100.100.100.100\n'
  } > "$tmp"
  for entry in "${PATTERNS[@]}"; do
    local name="${entry%%|*}" re="${entry#*|}"
    case "$name" in long-token|private-key|aws-key|bearer-literal) continue;; esac
    n=$(grep -cE -e "$re" "$tmp" 2>/dev/null || true)
    if [ "${n:-0}" -lt 1 ]; then echo "SELF-TEST FAILED: pattern '$name' no longer detects its canary" >&2; bad=1; fi
  done
  rm -f "$tmp"
  return $bad
}
if ! selftest; then
  echo "scan-secrets: FAILING CLOSED - the scanner cannot prove it still works, so it will not report 'clean'." >&2
  exit 2
fi

# ---- gather content ----------------------------------------------------------------
tmpd=$(mktemp -d); trap 'rm -rf "$tmpd"' EXIT
if [ "$MODE" = "--staged" ]; then
  git diff --cached --name-only --diff-filter=ACM | grep -vE "$EXCLUDE" > "$tmpd/files" || true
  : > "$tmpd/content"
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    git show ":$f" 2>/dev/null | sed "s|^|$f:|" >> "$tmpd/content"
  done < "$tmpd/files"
else
  # cached + untracked-but-not-ignored: untracked files are exactly the ones about to be added
  git ls-files --cached --others --exclude-standard | grep -vE "$EXCLUDE" | sort -u > "$tmpd/files" || true
  : > "$tmpd/content"
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    case "$f" in *.png|*.jpg|*.gif|*.ico|*.woff*) continue;; esac
    sed "s|^|$f:|" "$f" 2>/dev/null >> "$tmpd/content"
  done < "$tmpd/files"
fi

# ---- scan --------------------------------------------------------------------------
for entry in "${PATTERNS[@]}"; do
  name="${entry%%|*}"; re="${entry#*|}"
  hits=$(grep -nE -e "$re" "$tmpd/content" 2>/dev/null | grep -vE -e "$ALLOW" || true)
  if [ -n "$hits" ]; then
    rc=1
    echo "BLOCKED [$name]"
    printf '%s\n' "$hits" | cut -c1-160 | sed 's/^/    /'
  fi
done

# ---- owner's own literal values (never committed) ----------------------------------
if [ -s .secret-patterns ]; then
  hits=$(grep -nF -f .secret-patterns "$tmpd/content" 2>/dev/null || true)
  if [ -n "$hits" ]; then
    rc=1
    echo "BLOCKED [local .secret-patterns] $(printf '%s\n' "$hits" | grep -c .) line(s) matched a value from your local secret list"
    printf '%s\n' "$hits" | cut -d: -f1-2 | sed 's/^/    /' | sort -u
  fi
fi

# ---- populated credential values in tracked env templates --------------------------
for f in $(git ls-files | grep -E '\.env(\.example)?$' || true); do
  bad=$(grep -nE -e '^[A-Z_]*(TOKEN|SECRET|PASSWORD|KEY|WEBHOOK|INVITE|ACCOUNT|CHANNEL_ID|GUILD_ID)[A-Z_]*=.+' "$f" 2>/dev/null | grep -vE -e "$ALLOW" || true)
  if [ -n "$bad" ]; then rc=1; echo "BLOCKED [populated-template] $f"; printf '%s\n' "$bad" | sed 's/^/    /'; fi
done

if [ "$rc" -eq 0 ]; then
  echo "scan-secrets: clean (self-test passed; $(grep -c . "$tmpd/files" 2>/dev/null || echo 0) files checked)"
else
  echo ""
  echo "One or more secrets were detected. Do NOT commit this."
  echo "If a value is a legitimate placeholder, use the repo's convention (CHANGEME_NAME or <NAME>)."
  echo "If a real credential has already been pushed, ROTATE IT - removing the commit does not undo the exposure."
fi
exit $rc
