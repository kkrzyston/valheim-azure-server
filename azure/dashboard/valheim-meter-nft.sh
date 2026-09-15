#!/usr/bin/env bash
# valheim-meter-nft.sh -- install the `inet valheim_meter` nftables table: two byte/packet
# counters for game traffic, and a short-lived set of the addresses currently sending to the
# game port.
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
#
# Ports come from the environment (VALHEIM_GAME_PORT / VALHEIM_QUERY_PORT, defaults 2456/2457)
# so nothing installation-specific is baked in -- same convention as every other script here.
# VALHEIM_PEER_TIMEOUT (default 2m) is how long an address lingers in the `peers` set after it
# stops sending. It is deliberately short: the probe cross-checks its player count against the
# size of this set to catch a join that status.json has not caught up with yet, and a long
# timeout would leave departed players in the set for that whole window, making the check noisy.
# The collector only needs an address to be present while the player is actively sending, and an
# active Valheim client sends tens of packets a second, so 2m is generous for that purpose.
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
if ! [[ "$PEER_TIMEOUT" =~ ^[0-9]{1,4}[smh]$ ]]; then
  echo "valheim-meter-nft: VALHEIM_PEER_TIMEOUT=$PEER_TIMEOUT is not an nft timeout (e.g. 30s, 2m, 1h)" >&2
  exit 2
fi

case "$ACTION" in
  ensure|install|show|remove) ;;
  *) echo "usage: valheim-meter-nft.sh ensure|install|show|remove" >&2; exit 64 ;;
esac

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
      if printf '%s' "$have" | grep -q 'counter game_tx'          && printf '%s' "$have" | grep -q 'counter game_rx'          && printf '%s' "$have" | grep -q 'set peers'; then
        echo "valheim-meter-nft: table $TABLE already present and complete -- left alone (counters not reset)"
        exit 0
      fi
      echo "valheim-meter-nft: table $TABLE is present but incomplete -- rebuilding" >&2
    fi
    exec "$0" install
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
  counter game_tx { }
  counter game_rx { }

  # Addresses seen sending to the game port within VALHEIM_PEER_TIMEOUT. This replaces the
  # per-minute tcpdump the collector used to run purely to learn who to ping. Entries expire
  # on their own, so nothing has to prune them, and the set never grows past the player cap.
  # The probe also compares the size of this set against its player count, which is how a join
  # is noticed before status.json catches up -- see the header.
  set peers {
    type ipv4_addr
    flags timeout
    timeout $PEER_TIMEOUT
  }

  chain out {
    type filter hook output priority 300; policy accept;
    oifname != "lo" udp sport { $GAME_PORT, $QUERY_PORT } counter name game_tx
  }

  chain in {
    type filter hook input priority 300; policy accept;
    iifname != "lo" udp dport { $GAME_PORT, $QUERY_PORT } counter name game_rx
    iifname != "lo" udp dport $GAME_PORT update @peers { ip saddr }
  }
}
NFT
    echo "valheim-meter-nft: installed table $TABLE (game $GAME_PORT, query $QUERY_PORT)"
    ;;
esac
