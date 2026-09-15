#!/usr/bin/env bash
# valheim-peermeter-nft.sh -- install the `inet valheim_peermeter` nftables table: PER-PEER
# byte/packet counters for game traffic, keyed by the peer's IP address.
#
# valheim-meter-nft.sh (a separate script, a separate table, owned by a different piece of
# work) already answers "how many bytes total". This table answers a different question: is
# the ~270 KiB/s egress ceiling a GLOBAL budget shared across every connected player, or a
# PER-PEER budget that each player gets independently? Six aggregate counters cannot tell
# those apart -- you need bytes keyed by which peer they went to. nftables "dynamic sets with
# stateful per-element counters" (a "meter", in the nftables wiki's own term) do exactly this:
# `update @set { ip daddr counter }` creates or refreshes an element keyed by address and
# accumulates a counter on it, all in-kernel, no AF_PACKET tap needed. Verified empirically
# (not assumed from docs) against this box's nft v1.0.2 / kernel 6.8.0-1065-azure before this
# script was written: a scratch table with synthetic UDP traffic produced per-address elements
# whose byte counts matched IP+UDP header arithmetic exactly.
#
#   valheim-peermeter-nft.sh ensure    create the table ONLY if missing or malformed. Safe to
#                                      run from a unit's ExecStartPre= or repeatedly by hand;
#                                      never resets counters that are already there.
#   valheim-peermeter-nft.sh install   unconditionally create/replace the table (resets it).
#   valheim-peermeter-nft.sh show      print the current table (including per-peer elements).
#   valheim-peermeter-nft.sh remove    delete ONLY this table. Leaves every other table,
#                                      including valheim_meter, untouched.
#   valheim-peermeter-nft.sh selftest  stubbed-nft test, no root, no real nft needed.
#
# Ports come from the environment (VALHEIM_GAME_PORT, default 2456), same convention as
# valheim-meter-nft.sh. VALHEIM_PEER_TIMEOUT (default 2m) and VALHEIM_PEER_MAX (default 256)
# match that script's peer-set defaults for the same reasons: short enough that a departed
# player's element does not linger and skew a later per-peer read, capped so an internet-facing
# UDP port cannot be used to grow an unbounded kernel allocation via spoofed source addresses.
#
# KEYING -- this is the one design choice that matters and is easy to get backwards:
#   * egress (the direction with the ceiling) is counted in the OUTPUT chain, keyed by
#     ip daddr (destination) -- "how many bytes did we send TO this peer".
#   * ingress is counted in the INPUT chain, keyed by ip saddr (source) -- "how many bytes did
#     we receive FROM this peer".
#   Keying egress by daddr and ingress by saddr is deliberate and not interchangeable: it is
#   the only pairing where both sets are keyed by "which peer", not "which direction the
#   traffic happened to enter this host from". A meter keyed the other way round would still
#   run without error -- nft has no way to catch that mistake -- and would silently answer a
#   different, useless question ("how many bytes came FROM the address that happens to equal
#   MY OWN address on this socket", which for a server's egress chain is nonsensical).
#
# SAFETY -- same argument as valheim-meter-nft.sh, restated for this table:
#   * Separate table, separate name (`valheim_peermeter` vs `valheim_meter`). Deleting and
#     recreating this table can never touch valheim_meter, the Azure agent's `ip security`
#     table, or Tailscale's `ip filter` table -- nftables tables are independent namespaces.
#   * Same priority-300 filter/output and filter/input hooks, `policy accept`, no verdict
#     rule anywhere in this table. The worst failure mode is a wrong or missing count, never
#     a dropped or altered packet. Traffic is already fully decided by the time this table
#     sees it (standard filter is priority 0, security 50, srcnat 100).
#   * IPv4 only, matching valheim_meter's `peers` set. An IPv6 peer's bytes are simply not
#     counted here (same limitation valheim_meter documents for its own peer set) rather than
#     silently mixed into a v4 peer's bucket.
#   * `oifname != "lo"` / `iifname != "lo"` for the same reason valheim_meter uses it: this
#     host's own loopback queries to the local Steam A2S port must not appear as peer traffic.
set -euo pipefail
ACTION="${1:-install}"
TABLE="inet valheim_peermeter"

port() {
  local name="$1" val="$2"
  if ! [[ "$val" =~ ^[0-9]{1,5}$ ]] || [ "$val" -lt 1 ] || [ "$val" -gt 65535 ]; then
    echo "valheim-peermeter-nft: $name=$val is not a port number (1-65535)" >&2
    exit 2
  fi
  printf '%s' "$val"
}
GAME_PORT=$(port VALHEIM_GAME_PORT "${VALHEIM_GAME_PORT:-2456}")
PEER_TIMEOUT="${VALHEIM_PEER_TIMEOUT:-2m}"
PEER_MAX=$(port VALHEIM_PEER_MAX "${VALHEIM_PEER_MAX:-256}")
if ! [[ "$PEER_TIMEOUT" =~ ^[0-9]{1,4}[smh]$ ]]; then
  echo "valheim-peermeter-nft: VALHEIM_PEER_TIMEOUT=$PEER_TIMEOUT is not an nft timeout (e.g. 30s, 2m, 1h)" >&2
  exit 2
fi

do_install() {
  # Same add-then-delete-then-recreate atomic transaction as valheim-meter-nft.sh, for the
  # same reason: one `nft -f` either replaces the whole table or changes nothing.
  nft -f - <<NFT
table $TABLE
delete table $TABLE

table $TABLE {
  # UNQUOTED HEREDOC BODY -- same hazard as valheim-meter-nft.sh's install function. No
  # backtick, no dollar-paren, in any comment or rule below. Run selftest before shipping an
  # edit here; it checks this file's own source for both.
  #
  # peer_tx: bytes/packets THIS HOST SENT, keyed by destination peer. This is the number that
  # answers the actual question (the ceiling is on egress), so it is sized and timed out the
  # same as valheim_meter's peers set rather than treated as a secondary metric.
  set peer_tx {
    type ipv4_addr
    flags dynamic,timeout
    timeout $PEER_TIMEOUT
    size $PEER_MAX
  }

  # peer_rx: bytes/packets THIS HOST RECEIVED, keyed by source peer. Kept for completeness and
  # for spotting an asymmetric peer (e.g. one client spamming inputs), but egress is the
  # number the 270 KiB/s ceiling is about.
  set peer_rx {
    type ipv4_addr
    flags dynamic,timeout
    timeout $PEER_TIMEOUT
    size $PEER_MAX
  }

  chain out {
    type filter hook output priority 300; policy accept;
    oifname != "lo" meta nfproto ipv4 udp sport $GAME_PORT update @peer_tx { ip daddr counter }
  }

  chain in {
    type filter hook input priority 300; policy accept;
    iifname != "lo" meta nfproto ipv4 udp dport $GAME_PORT update @peer_rx { ip saddr counter }
  }
}
NFT
  echo "valheim-peermeter-nft: installed table $TABLE (game $GAME_PORT, timeout $PEER_TIMEOUT, max $PEER_MAX)"
}

case "$ACTION" in
  ensure|install|show|remove|selftest) ;;
  *) echo "usage: valheim-peermeter-nft.sh ensure|install|show|remove|selftest" >&2; exit 64 ;;
esac

# --- selftest: stubbed nft, no root, touches no real ruleset -------------------------------
if [ "$ACTION" = "selftest" ]; then
  d=$(mktemp -d)
  fails=0
  mkdir -p "$d/bin"
  cat > "$d/bin/nft" <<'STUB'
#!/usr/bin/env bash
if [ "${1:-}" = "-f" ]; then cat > "$VMNFT_CAPTURE"; exit 0; fi
if [ "${1:-}" = "list" ] && [ "${2:-}" = "table" ]; then
  if [ -f "$VMNFT_WARM" ]; then cat "$VMNFT_WARM"; exit 0; fi
  exit 1
fi
if [ "${1:-}" = "list" ] && [ "${2:-}" = "tables" ]; then
  if [ -f "$VMNFT_WARM" ]; then echo "table inet valheim_peermeter"; fi
  exit 0
fi
exit 0
STUB
  chmod +x "$d/bin/nft"
  for w in udp ip size meta oifname iifname counter policy priority hook filter accept update; do
    printf '#!/usr/bin/env bash\necho POISON-%s\necho POISON-%s >&2\n' "$w" "$w" > "$d/bin/$w"
    chmod +x "$d/bin/$w"
  done
  cat > "$d/warm-fixture" <<'WARM'
table inet valheim_peermeter {
  set peer_tx { type ipv4_addr }
  set peer_rx { type ipv4_addr }
}
WARM

  run_case() {
    label="$1"; action="$2"
    : > "$d/err"; : > "$d/out"
    if PATH="$d/bin:$PATH" VMNFT_CAPTURE="$d/cap" VMNFT_WARM="$d/warm" \
         bash "$0" "$action" > "$d/out" 2> "$d/err"; then :; else
      echo "  FAIL $label: exited non-zero"; fails=$((fails + 1))
    fi
    if [ -s "$d/err" ]; then
      echo "  FAIL $label: wrote to stderr -- a stray expansion, almost certainly:"
      sed 's/^/           /' "$d/err"
      fails=$((fails + 1))
    else
      echo "  ok   $label: stderr clean"
    fi
  }

  echo "cold path (no table yet)"
  rm -f "$d/warm" "$d/cap"
  run_case "ensure builds the table" ensure
  echo "warm path (table already present and complete)"
  cp "$d/warm-fixture" "$d/warm"; rm -f "$d/cap"
  run_case "ensure leaves it alone" ensure
  if [ -f "$d/cap" ]; then
    echo "  FAIL warm ensure rewrote the table; that would reset live per-peer counters"
    fails=$((fails + 1))
  else
    echo "  ok   warm ensure did not rewrite (counters survive a restart)"
  fi
  echo "install and remove"
  rm -f "$d/warm" "$d/cap"
  run_case "install" install
  run_case "remove" remove

  echo "the generated ruleset"
  rm -f "$d/warm" "$d/cap"
  PATH="$d/bin:$PATH" VMNFT_CAPTURE="$d/cap" VMNFT_WARM="$d/warm" bash "$0" install >/dev/null 2>&1 || true
  if [ ! -s "$d/cap" ]; then
    echo "  FAIL nothing was generated"; fails=$((fails + 1))
  else
    if grep -q "update @peer_tx { ip daddr counter }" "$d/cap"; then
      echo "  ok   egress is keyed by destination (ip daddr)"
    else
      echo "  FAIL egress rule missing or not keyed by ip daddr"; fails=$((fails + 1))
    fi
    if grep -q "update @peer_rx { ip saddr counter }" "$d/cap"; then
      echo "  ok   ingress is keyed by source (ip saddr)"
    else
      echo "  FAIL ingress rule missing or not keyed by ip saddr"; fails=$((fails + 1))
    fi
    if grep -q "POISON" "$d/cap"; then
      echo "  FAIL a substitution ran in the heredoc and its output reached the ruleset:"
      grep -n "POISON" "$d/cap" | sed 's/^/           /'
      fails=$((fails + 1))
    else
      echo "  ok   no command substitution reached the ruleset"
    fi
    if grep -q '[$]' "$d/cap"; then
      echo "  FAIL an unexpanded variable survived into the ruleset:"
      grep -n '[$]' "$d/cap" | sed 's/^/           /'; fails=$((fails + 1))
    else
      echo "  ok   every variable interpolated"
    fi
  fi

  echo "this file's own source (the authoring guard)"
  awk '/<<NFT$/{inh=1;next} inh && /^NFT$/{inh=0;next} inh{print}' "$0" > "$d/bodies"
  if grep -q '[`]' "$d/bodies"; then
    echo "  FAIL a backtick sits inside an unquoted heredoc body; bash executes it as root:"
    grep -n '[`]' "$d/bodies" | sed 's/^/           /'; fails=$((fails + 1))
  else
    echo "  ok   no backtick inside any unquoted heredoc body"
  fi
  if grep -q '[$](' "$d/bodies"; then
    echo "  FAIL a command substitution sits inside an unquoted heredoc body; same hazard:"
    grep -n '[$](' "$d/bodies" | sed 's/^/           /'; fails=$((fails + 1))
  else
    echo "  ok   no command substitution inside any unquoted heredoc body"
  fi
  stray=$(grep -o '[$][A-Za-z_][A-Za-z_]*' "$d/bodies" | sort -u \
          | grep -vxE '[$](TABLE|GAME_PORT|PEER_TIMEOUT|PEER_MAX)' || true)
  if [ -n "$stray" ]; then
    echo "  FAIL an unintended variable is expanded in a heredoc body: $stray"; fails=$((fails + 1))
  else
    echo "  ok   only the intended variables are expanded"
  fi

  rm -rf "$d"
  echo ""
  if [ "$fails" -eq 0 ]; then echo "selftest passed"; exit 0; fi
  echo "selftest FAILED ($fails)"; exit 1
fi

if ! command -v nft >/dev/null; then
  echo "valheim-peermeter-nft: nft is not installed (apt-get install -y nftables)" >&2
  exit 3
fi

case "$ACTION" in
  ensure)
    if have=$(nft list table $TABLE 2>/dev/null); then
      if printf '%s' "$have" | grep -q 'set peer_tx' && printf '%s' "$have" | grep -q 'set peer_rx'; then
        echo "valheim-peermeter-nft: table $TABLE already present and complete -- left alone (counters not reset)"
        exit 0
      fi
      echo "valheim-peermeter-nft: table $TABLE is present but incomplete -- rebuilding" >&2
    fi
    do_install
    ;;
  show)
    exec nft list table $TABLE
    ;;
  remove)
    nft -f - <<NFT
table $TABLE
delete table $TABLE
NFT
    echo "valheim-peermeter-nft: removed table $TABLE"
    ;;
  install)
    do_install
    ;;
esac
