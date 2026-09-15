#!/usr/bin/env bash
# valheim-meter-nft.sh -- install the `inet valheim_meter` nftables table: byte/packet counters
# for game traffic (split by port and by address family -- see the ruleset below for why that
# split is load-bearing rather than fussy), and a short-lived, size-capped set of the addresses
# currently sending to the game port.
#
# This table is the measurement primitive for the egress probe. It exists so that
# valheim-egress-probe.py can read an exact byte and packet count for game traffic once a
# second without an AF_PACKET tap (tcpdump) on the game's hot receive path, and so the
# collector can learn peer addresses the same way. Two rules and a set element update per
# packet is a few tens of nanoseconds; a packet tap is a copy to userspace.
#
#   valheim-meter-nft.sh ensure    create the table ONLY if it is missing or malformed. This is
#                                  what ExecStartPre= runs: a probe that crash-loops on a bad
#                                  evening must not flush the counters and the collector's peer
#                                  set every RestartSec. The probe reads counters as deltas, so
#                                  it has no need for them to start at zero.
#   valheim-meter-nft.sh install   unconditionally create/replace the table, resetting counters.
#   valheim-meter-nft.sh show      print the current table
#   valheim-meter-nft.sh remove    delete the table (leaves every other table untouched)
#   valheim-meter-nft.sh selftest  run against a stubbed nft: proves the cold and warm paths
#                                  write nothing to stderr and that no command substitution
#                                  hides in a heredoc body. No root and no real nft needed.
#
# Ports come from the environment (VALHEIM_GAME_PORT / VALHEIM_QUERY_PORT, defaults 2456/2457)
# so nothing installation-specific is baked in -- same convention as every other script here.
# VALHEIM_PEER_TIMEOUT (default 2m) is how long an address lingers in the `peers` set after it
# stops sending. It is deliberately short: the probe cross-checks its player count against the
# size of this set to catch a join that status.json has not caught up with yet, and a long
# timeout would leave departed players in the set for that whole window, making the check noisy.
# The collector only needs an address to be present while the player is actively sending, and an
# active Valheim client sends tens of packets a second, so 2m is generous for that purpose.
# VALHEIM_PEER_MAX (default 256) caps the set: the game port faces the internet, and an
# uncapped set is an unbounded kernel allocation driven by anyone who can spoof a source address.
#
# SAFETY -- why this cannot change what the server does with a packet:
#   * Separate table. nftables tables are independent rule sets; `table inet valheim_meter`
#     collides with nothing. The VM already carries `table ip security` (the Azure agent) and
#     `table ip filter` (Tailscale's ts-input/ts-forward chains); those are in the `ip` family
#     under different names, and ufw is inactive. Deleting and recreating OUR table never
#     touches theirs.
#   * The chains hook filter/output and filter/input at priority 300 -- after all of the
#     existing filtering (standard filter is 0, security 50, srcnat 100), so every verdict has
#     already been reached by the time we count.
#   * `policy accept`, and not one rule carries a verdict. A packet can only leave these
#     chains the way it arrived. The worst failure mode of a bug in this file is a wrong
#     number, never a dropped packet.
#
# `oifname != "lo"` / `iifname != "lo"` keep loopback out of the counters on purpose. The
# collector and the probe both query the Steam A2S port on 127.0.0.1, and counting our own
# measurement traffic as game egress would put a ~300 B/s sawtooth into exactly the flatness
# statistic the experiment turns on.
set -euo pipefail
ACTION="${1:-install}"
TABLE="inet valheim_meter"

# The ports are interpolated into an nft script, so they are validated as integers first
# rather than trusted: /etc/valheim-server.env is operator-written, but a typo there should
# fail loudly here instead of turning into a syntactically creative rule set.
port() {
  local name="$1" val="$2"
  if ! [[ "$val" =~ ^[0-9]{1,5}$ ]] || [ "$val" -lt 1 ] || [ "$val" -gt 65535 ]; then
    echo "valheim-meter-nft: $name=$val is not a port number (1-65535)" >&2
    exit 2
  fi
  printf '%s' "$val"
}
GAME_PORT=$(port VALHEIM_GAME_PORT "${VALHEIM_GAME_PORT:-2456}")
QUERY_PORT=$(port VALHEIM_QUERY_PORT "${VALHEIM_QUERY_PORT:-2457}")
PEER_TIMEOUT="${VALHEIM_PEER_TIMEOUT:-2m}"
PEER_MAX=$(port VALHEIM_PEER_MAX "${VALHEIM_PEER_MAX:-256}")
if ! [[ "$PEER_TIMEOUT" =~ ^[0-9]{1,4}[smh]$ ]]; then
  echo "valheim-meter-nft: VALHEIM_PEER_TIMEOUT=$PEER_TIMEOUT is not an nft timeout (e.g. 30s, 2m, 1h)" >&2
  exit 2
fi

# The install lives in a function rather than in the case arm, and `ensure` CALLS it rather
# than re-exec-ing this script. `exec "$0" install` looked tidier and was a trap: when $0
# carries no slash -- `bash valheim-meter-nft.sh ensure`, which is exactly how an operator
# tests it by hand -- exec falls back to a PATH search, does not find it, and the cold path
# dies with "not found". It worked from the unit only because systemd passes an absolute
# path. A plain function call has no such dependency on how the script was invoked.
do_install() {
  # One `nft -f` is one atomic transaction: either the whole table is replaced or nothing
  # changes. The bare `table` line adds it when absent and is a no-op when present, which
  # is what makes the `delete` below safe on a first run -- and makes re-running this on
  # every service start (ExecStartPre=) reset the counters to a known zero rather than
  # accumulating across restarts. It does NOT depend on nftables.service having run, so it
  # survives a reboot on its own.
  nft -f - <<NFT
table $TABLE
delete table $TABLE

table $TABLE {
  # EVERY LINE FROM HERE TO THE CLOSING NFT IS AN UNQUOTED HEREDOC BODY, COMMENTS INCLUDED.
  # The delimiter cannot be quoted: the ruleset genuinely needs TABLE, GAME_PORT, QUERY_PORT,
  # PEER_TIMEOUT and PEER_MAX interpolated. That also means bash performs COMMAND SUBSTITUTION
  # in here. nft comments are not markdown -- a backtick, or a dollar sign before a paren, is
  # EXECUTED AS ROOT on every boot through ExecStartPre=. That shipped once, in three comment
  # lines. It was harmless only by luck, because those three backticked words happened not to
  # be commands and substituted empty strings; one that resolved to a real program would have
  # silently corrupted the ruleset instead. Run selftest (it is in this file) before shipping
  # an edit in here: it fails if either construct reappears, which bash -n cannot detect
  # because this is a runtime expansion and not a syntax error.
  #
  # SEPARATE COUNTERS PER PORT AND PER ADDRESS FAMILY, because the analysis models exactly one
  # of them. predicted_ceiling() describes per-peer ZDO traffic on the GAME port only. Folding
  # the Steam query port into the same counter would let a master-server scrape inflate the
  # measured side of a comparison whose whole point is whether a measured value sits above a
  # computed one -- the same reason the header size is 28 and not 42.
  counter game_tx { }
  counter game_rx { }
  counter query_tx { }
  counter query_rx { }
  # And the v4/v6 split is not pedantry. A bare udp sport match covers both address families,
  # but the peer set below is ipv4_addr and an ip saddr match only ever matches v4 -- so an
  # IPv6 player used to have
  # their bytes counted while the peer set denied they existed. That desynchronises bytes from
  # player count in precisely the per-player scaling R2 turns on, and silently drops their ping
  # from the dashboard. The model now counts v4 only; v6 is counted separately so that it is
  # VISIBLE rather than silently mixed in, and the probe warns if it is ever non-zero.
  counter game_tx6 { }
  counter game_rx6 { }

  # Addresses seen sending to the game port within VALHEIM_PEER_TIMEOUT. This replaces the
  # per-minute tcpdump the collector used to run purely to learn who to ping. Entries expire on
  # their own, so nothing has to prune them.
  #
  # A size cap is mandatory, not tidiness: this is an INTERNET-FACING port. Without one, every
  # scanner that poked UDP $GAME_PORT once would sit here until it expired, and a spoofed-source
  # flood would grow the set unbounded in kernel memory. With a cap, the kernel refuses new
  # elements instead -- the players already in the set keep working, and the collector bounds the
  # ping fan-out on its own side too.
  set peers {
    type ipv4_addr
    flags timeout
    timeout $PEER_TIMEOUT
    size $PEER_MAX
  }

  chain out {
    type filter hook output priority 300; policy accept;
    oifname != "lo" meta nfproto ipv4 udp sport $GAME_PORT counter name game_tx
    oifname != "lo" meta nfproto ipv6 udp sport $GAME_PORT counter name game_tx6
    oifname != "lo" udp sport $QUERY_PORT counter name query_tx
  }

  chain in {
    type filter hook input priority 300; policy accept;
    iifname != "lo" meta nfproto ipv4 udp dport $GAME_PORT counter name game_rx
    iifname != "lo" meta nfproto ipv6 udp dport $GAME_PORT counter name game_rx6
    iifname != "lo" udp dport $QUERY_PORT counter name query_rx
    iifname != "lo" meta nfproto ipv4 udp dport $GAME_PORT update @peers { ip saddr }
  }
}
NFT
  echo "valheim-meter-nft: installed table $TABLE (game $GAME_PORT, query $QUERY_PORT)"
}

case "$ACTION" in
  ensure|install|show|remove|selftest) ;;
  *) echo "usage: valheim-meter-nft.sh ensure|install|show|remove|selftest" >&2; exit 64 ;;
esac

# --- selftest: stubbed nft, no root, touches no real ruleset -------------------------------
# It exists for one defect class that bash -n structurally cannot catch, because it is a runtime
# expansion rather than a syntax error: an unquoted heredoc body performs command substitution,
# so a backtick in an nft COMMENT is executed, as root, on every boot. Same lesson as the literal
# backslash-n that once aborted install-dashboard.sh halfway -- syntax-checking a script is not
# the same as running it.
#
#   * stderr must be EMPTY on the cold and warm paths. Any stray substitution announces itself
#     there, either as "command not found" or as some real command's usage message. This is the
#     load-bearing assertion, and it catches the general case.
#   * no backtick and no dollar-paren may survive inside an unquoted heredoc body, checked
#     against this file's own source. That catches an authoring mistake whose command would have
#     succeeded quietly, which the stderr check alone would miss.
#   * poison executables named after nft vocabulary sit on PATH, so a substitution that WOULD
#     have succeeded silently instead lands a visible marker in the generated ruleset.
if [ "$ACTION" = "selftest" ]; then
  d=$(mktemp -d)
  fails=0
  mkdir -p "$d/bin"
  # The stub writes to stderr NEVER: the test is that stderr stays empty, so the instrument must
  # not be the thing that dirties it.
  cat > "$d/bin/nft" <<'STUB'
#!/usr/bin/env bash
if [ "${1:-}" = "-f" ]; then cat > "$VMNFT_CAPTURE"; exit 0; fi
if [ "${1:-}" = "list" ] && [ "${2:-}" = "table" ]; then
  if [ -f "$VMNFT_WARM" ]; then cat "$VMNFT_WARM"; exit 0; fi
  exit 1
fi
if [ "${1:-}" = "list" ] && [ "${2:-}" = "tables" ]; then
  if [ -f "$VMNFT_WARM" ]; then echo "table inet valheim_meter"; fi
  exit 0
fi
exit 0
STUB
  chmod +x "$d/bin/nft"
  for w in udp ip size meta oifname iifname counter policy priority hook filter accept; do
    printf '#!/usr/bin/env bash\necho POISON-%s\necho POISON-%s >&2\n' "$w" "$w" > "$d/bin/$w"
    chmod +x "$d/bin/$w"
  done
  cat > "$d/warm-fixture" <<'WARM'
table inet valheim_meter {
  counter game_tx { packets 1 bytes 1 }
  counter game_rx { packets 1 bytes 1 }
  counter query_tx { packets 0 bytes 0 }
  counter game_tx6 { packets 0 bytes 0 }
  set peers { type ipv4_addr }
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

  echo "cold path (no table yet -- this is what runs at every boot)"
  rm -f "$d/warm" "$d/cap"
  run_case "ensure builds the table" ensure
  echo "warm path (table already present and complete)"
  cp "$d/warm-fixture" "$d/warm"; rm -f "$d/cap"
  run_case "ensure leaves it alone" ensure
  if [ -f "$d/cap" ]; then
    echo "  FAIL warm ensure rewrote the table; that flushes live counters and the peer set"
    fails=$((fails + 1))
  else
    echo "  ok   warm ensure did not rewrite (counters and peer set survive a restart)"
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
    n=$(grep -c "counter name" "$d/cap" || true)
    if [ "$n" -eq 6 ]; then echo "  ok   six counter rules"
    else echo "  FAIL expected 6 counter rules, generated $n"; fails=$((fails + 1)); fi
    if grep -q "update @peers" "$d/cap"; then echo "  ok   the peer-set rule is present"
    else echo "  FAIL the peer-set rule is missing"; fails=$((fails + 1)); fi
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
          | grep -vxE '[$](TABLE|GAME_PORT|QUERY_PORT|PEER_TIMEOUT|PEER_MAX)' || true)
  if [ -n "$stray" ]; then
    echo "  FAIL an unintended variable is expanded in a heredoc body: $stray"; fails=$((fails + 1))
  else
    echo "  ok   only the five intended variables are expanded"
  fi

  rm -rf "$d"
  echo ""
  if [ "$fails" -eq 0 ]; then echo "selftest passed"; exit 0; fi
  echo "selftest FAILED ($fails)"; exit 1
fi

if ! command -v nft >/dev/null; then
  echo "valheim-meter-nft: nft is not installed (apt-get install -y nftables)" >&2
  exit 3
fi

case "$ACTION" in
  ensure)
    # A table that exists but is missing a counter or the set is worse than no table: the probe
    # would read a partial ruleset and the collector would silently lose peer discovery. So
    # "present" is not enough -- check that all three objects are actually there before deciding
    # to leave it alone.
    if have=$(nft list table $TABLE 2>/dev/null); then
      if printf '%s' "$have" | grep -q 'counter game_tx'          && printf '%s' "$have" | grep -q 'counter game_rx'          && printf '%s' "$have" | grep -q 'counter query_tx'          && printf '%s' "$have" | grep -q 'counter game_tx6'          && printf '%s' "$have" | grep -q 'set peers'; then
        echo "valheim-meter-nft: table $TABLE already present and complete -- left alone (counters not reset)"
        exit 0
      fi
      echo "valheim-meter-nft: table $TABLE is present but incomplete -- rebuilding" >&2
    fi
    do_install
    ;;
  show)
    exec nft list table $TABLE
    ;;
  remove)
    # Same add-then-delete trick as install: `delete` on a table that was never there is an
    # error, so create it first and the delete always has something to remove.
    nft -f - <<NFT
table $TABLE
delete table $TABLE
NFT
    echo "valheim-meter-nft: removed table $TABLE"
    ;;
  install)
    do_install
    ;;
esac
