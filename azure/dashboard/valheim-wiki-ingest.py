#!/usr/bin/env python3
"""valheim-wiki-ingest.py -- offline ingestion of the Valheim Fandom and Weird Gloop wikis into
wiki-records.jsonl, the flat per-heading corpus task W2's index build reads. See PLAN-v6.md
(section "W1 -- Ingestion") for the authoritative spec; this docstring covers usage, not rationale.

Run this in its OWN venv (requirements-ingest.txt), never inside the bot's runtime -- PLAN-v6.md's
"Hard rules for every agent" is explicit that valheim-bot.py stays stdlib-only, and this script's
one non-stdlib dependency (mwparserfromhell) is exactly the kind of thing that must never leak
into that import graph.

WHAT THIS DOES, IN ORDER (see PLAN-v6.md W1 for the numbered steps this mirrors):
  1. Fandom base   -- download & decompress the published bulk XML dump, stream-parse it, ns0 only.
  2. Fandom delta  -- walk Fandom's API (generator=allpages, batched) and keep only pages whose
                       live revision is newer than what the dump had for that exact title.
  3. Weird Gloop   -- the same batched allpages walk, covering all of its ns0 pages.
  4. Parse every page's wikitext with mwparserfromhell (never regex) and split it into per-heading
     sections, rendering infobox templates -- including ones nested inside {{InfoboxTabber}} --
     into readable "key: value" prose lines.
  5. Sanitize each section's text (strip refs/comments/file-and-category links/@everyone/@here,
     collapse whitespace).
  6. Emit wiki-records.jsonl, one JSON object per line, sorted by (title, heading, source) so a
     re-run with no real content change is byte-identical.

FAILURE POLICY (a contract with W4's systemd unit -- see PLAN-v6.md, corrected after W4 flagged
the original wording as self-contradictory):
  - A page that fails to parse is logged with its title and skipped, never silently dropped.
  - The run ALWAYS exits 0 when it successfully writes a corpus file, no matter how many pages
    were skipped below the 5% threshold -- the skip count lives in the log and the final summary
    line, never in the exit status. valheim-wiki-refresh.service (task W4) chains this script and
    the index build as two ExecStart lines in one unit; systemd stops at the first non-zero exit,
    so encoding a benign skip count as a non-zero exit would silently cancel the weekly index
    rebuild while the unit still looked healthy.
  - The run exits non-zero ONLY on the hard-failure path: more than 5% of the pages it attempted
    failed to parse, or the output file could not be written. Either way it writes nothing --
    a half-built corpus is worse than a stale one, and this is the one case where stopping the
    downstream index build is exactly correct.
  - The final summary line is always emitted, at INFO on a clean run and WARNING when any page
    was skipped, and always includes the literal token "SUMMARY" so `journalctl -u
    valheim-wiki-refresh | grep SUMMARY` finds it -- a slowly-rising skip count across weekly runs
    is the only early warning that a wiki changed its template structure under us, and that is
    useless if a human has to read full-verbosity logs to notice it.

USAGE:
    python valheim-wiki-ingest.py [--out PATH] [--limit N] [--offline DIR] [--dump-path PATH]
                                   [--contact TEXT] [-v]

    --out PATH        Output path for the JSONL corpus. Default: wiki-records.jsonl next to this
                       script.
    --limit N         Cap the number of pages read from EACH of the three sources (dump stream,
                       Fandom delta walk, Weird Gloop walk) at N. Exists so this script can be
                       exercised, including against the live wikis, without pulling the full
                       ~1,179 + ~1,034 page corpus -- `--limit 20` touches at most ~60 pages total
                       across all three sources, not 20 combined. Omit for a full production run.
    --offline DIR      Read canned fixtures from DIR instead of touching the network at all: no
                       download, no decompression, no HTTP request of any kind. See
                       `read_offline_fixtures()` below for the exact file layout DIR must have.
                       Exists so this script's behavior (parsing, sanitizing, sorting, the failure
                       policy) can be verified in an environment with no network access, or in CI.
    --dump-path PATH   Reuse an already-downloaded copy of the Fandom .xml.7z dump instead of
                       fetching it again -- handy while iterating locally. Skipped entirely when
                       --offline is given.
    --contact TEXT     Overrides the contact string embedded in the descriptive User-Agent (see
                       BASE_USER_AGENT below) sent with every live HTTP request. Also settable via
                       the WIKI_INGEST_CONTACT environment variable (this flag wins if both are
                       given). Meaningless with --offline.
    -v / --verbose     DEBUG-level logging instead of INFO.

Exit codes: 0 = wrote a corpus file (see FAILURE POLICY above for what "0" does and does not
mean); 1 = hard failure (see above) -- nothing was written; 2 = usage/argument error (argparse's
own default).
"""

import argparse
import io
import json
import logging
import os
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Iterator, Optional

try:
    import requests
except ImportError:  # pragma: no cover -- caught at startup, see main()
    requests = None

try:
    import mwparserfromhell
except ImportError:  # pragma: no cover -- caught at startup, see main()
    mwparserfromhell = None

try:
    import py7zr
except ImportError:  # pragma: no cover -- only needed for the live Fandom-dump path
    py7zr = None


# ---------------------------------------------------------------- constants (sources & etiquette)
# Both endpoints per PLAN-v6.md's "Sources, licensing and etiquette" table -- decided, not
# re-litigated here. Weird Gloop has NO "/w/" prefix; Fandom does use the ordinary /api.php form.
FANDOM_API = "https://valheim.fandom.com/api.php"
WEIRDGLOOP_API = "https://valheim.weirdgloop.org/api.php"
FANDOM_DUMP_URL = "https://s3.amazonaws.com/wikia_xml_dumps/v/va/valheim_pages_current.xml.7z"
FANDOM_BASE_URL = "https://valheim.fandom.com/wiki/"
WEIRDGLOOP_BASE_URL = "https://valheim.weirdgloop.org/w/"

# PLAN-v6.md: "Weird Gloop's robots.txt disallows /*api.php for all agents and names ClaudeBot
# explicitly. The owner has decided to fetch anyway. Therefore: identify with a descriptive
# User-Agent naming this project and a contact address -- never a browser spoof, never ClaudeBot."
#
# DEVIATION, stated plainly in this task's report: the plan says "a contact address" without
# specifying whose. This script defaults the contact token to the project's own public repository
# URL rather than a personal email address -- both wikis' operators can reach the project (open an
# issue) through it, it satisfies MediaWiki API etiquette (a way to reach *someone* responsible for
# the traffic) exactly as well as an email would, and it avoids putting a personal address in an
# HTTP header sent, on the owner's own explicit decision, to a site whose robots.txt asks this
# project's bots to stay away. WIKI_INGEST_CONTACT (env var) or --contact overrides this with a
# real address if the owner prefers one.
DEFAULT_CONTACT = "https://github.com/kkrzyston/valheim-azure-server"
BASE_USER_AGENT = "westernskies-hermodr-wiki-ingest/1.0 (Valheim Discord bot knowledge base; {contact})"

MAXLAG = 5  # seconds; passed on every write-cheap read request per PLAN-v6.md's etiquette rule
GAP_LIMIT = 500  # "Anonymous gaplimit 500/request" per PLAN-v6.md's source table, for both wikis
REQUEST_TIMEOUT_S = 30
MAX_RETRIES = 5
BACKOFF_BASE_S = 2.0  # doubles each retry: 2, 4, 8, 16, 32s, capped by MAX_RETRIES
MIN_REQUEST_INTERVAL_S = 1.0  # serialized: never more than one request/second to either wiki

FAILURE_THRESHOLD = 0.05  # >5% of attempted pages failing to parse => hard failure, write nothing

# Sections rendered from an infobox template ("infobox creature", "infobox armor", ...) are never
# the wrapper template itself -- that one only carries positional (label, content) pairs, no
# key/value data of its own. Matched by prefix, case-insensitively, on the template's own name.
INFOBOX_NAME_PREFIX = "infobox"
INFOBOX_TABBER_NAME = "infoboxtabber"

_LOG = logging.getLogger("valheim-wiki-ingest")


# ---------------------------------------------------------------- small shared value types
@dataclass
class RawPage:
    """One page as read from any source, before section-splitting. `timestamp` is the ISO-8601
    string MediaWiki hands back (e.g. "2026-09-13T05:00:25Z") -- kept as a string throughout since
    every consumer either logs it or writes it straight into JSON; nothing here does date math."""

    title: str
    wikitext: str
    revid: int
    timestamp: str
    source: str  # "fandom" | "weirdgloop"


@dataclass
class Record:
    """One output line. Field order here is also the field order written to JSON, matching W2's
    documented record shape exactly (see PLAN-v6.md's "interface W2 and W3 agree on up front")."""

    title: str
    heading: str
    text: str
    source: str
    revid: int
    timestamp: str
    url: str

    def sort_key(self):
        # PLAN-v6.md step 7: sorted by (title, heading, source) for a byte-stable re-run.
        return (self.title, self.heading, self.source)

    def to_json_line(self) -> str:
        return json.dumps(
            {
                "title": self.title,
                "heading": self.heading,
                "text": self.text,
                "source": self.source,
                "revid": self.revid,
                "timestamp": self.timestamp,
                "url": self.url,
            },
            ensure_ascii=False,
            sort_keys=True,
        )


@dataclass
class RunStats:
    """Accumulated across the whole run so the final summary line (see module docstring's FAILURE
    POLICY) and the exit-code decision both read from one place instead of threading counters
    through every function separately."""

    attempted: int = 0
    succeeded: int = 0
    skipped_titles: list = field(default_factory=list)

    @property
    def skipped(self) -> int:
        return len(self.skipped_titles)

    def skip_ratio(self) -> float:
        return (self.skipped / self.attempted) if self.attempted else 0.0

    def record_success(self):
        self.attempted += 1
        self.succeeded += 1

    def record_skip(self, title: str, reason: str):
        self.attempted += 1
        self.skipped_titles.append(title)
        _LOG.warning("skip: page %r failed to parse -- %s", title, reason)


def build_user_agent(contact: Optional[str]) -> str:
    resolved = (contact or os.environ.get("WIKI_INGEST_CONTACT", "").strip() or DEFAULT_CONTACT)
    return BASE_USER_AGENT.format(contact=resolved)


# ---------------------------------------------------------------- etiquette-compliant HTTP client
class WikiClient:
    """One `requests.Session` per wiki host, holding this run's User-Agent and enforcing the
    etiquette PLAN-v6.md requires for hitting an endpoint whose robots.txt says no: `maxlag`,
    serialized requests (never more than one in flight, and never faster than
    MIN_REQUEST_INTERVAL_S apart), and exponential backoff on 429/503. Never a browser
    User-Agent, never "ClaudeBot" -- see build_user_agent()."""

    def __init__(self, base_url: str, user_agent: str):
        self.base_url = base_url
        self._last_request_ts = 0.0
        self._session = requests.Session() if requests is not None else None
        if self._session is not None:
            self._session.headers["User-Agent"] = user_agent

    def _throttle(self):
        elapsed = time.monotonic() - self._last_request_ts
        wait = MIN_REQUEST_INTERVAL_S - elapsed
        if wait > 0:
            time.sleep(wait)

    def get(self, params: dict) -> dict:
        """One GET against this wiki's api.php, with maxlag set, serialized against this client's
        own last request, and retried with exponential backoff on 429/503 or a MediaWiki
        maxlag-exceeded error (mirrored in the JSON body, not just the status line -- MediaWiki
        answers maxlag breaches with HTTP 200 and an `error.code == "maxlag"` payload). Raises
        requests.RequestException (or, if `requests` itself failed to import, RuntimeError) after
        MAX_RETRIES exhausted -- callers decide whether that page counts as a skip."""
        if self._session is None:
            raise RuntimeError(
                "the 'requests' package is not installed in this environment -- see "
                "requirements-ingest.txt; this script must run inside its own venv."
            )
        full_params = dict(params)
        full_params.setdefault("maxlag", MAXLAG)
        last_exc = None
        for attempt in range(MAX_RETRIES):
            self._throttle()
            self._last_request_ts = time.monotonic()
            try:
                resp = self._session.get(
                    self.base_url, params=full_params, timeout=REQUEST_TIMEOUT_S
                )
            except requests.RequestException as exc:
                last_exc = exc
                _LOG.warning(
                    "request to %s failed (attempt %d/%d): %r",
                    self.base_url, attempt + 1, MAX_RETRIES, exc,
                )
                time.sleep(BACKOFF_BASE_S * (2 ** attempt))
                continue

            if resp.status_code in (429, 503):
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else (
                    BACKOFF_BASE_S * (2 ** attempt)
                )
                _LOG.warning(
                    "%s returned %d (attempt %d/%d) -- backing off %.1fs",
                    self.base_url, resp.status_code, attempt + 1, MAX_RETRIES, delay,
                )
                time.sleep(delay)
                continue

            resp.raise_for_status()
            data = resp.json()
            error = data.get("error") if isinstance(data, dict) else None
            if error and error.get("code") == "maxlag":
                delay = BACKOFF_BASE_S * (2 ** attempt)
                _LOG.warning(
                    "%s reports maxlag exceeded (attempt %d/%d) -- backing off %.1fs",
                    self.base_url, attempt + 1, MAX_RETRIES, delay,
                )
                time.sleep(delay)
                continue
            return data

        raise last_exc or RuntimeError(
            f"{self.base_url}: exhausted {MAX_RETRIES} retries with no successful response"
        )


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return run(args)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Ingest the Valheim Fandom + Weird Gloop wikis into wiki-records.jsonl.",
    )
    default_out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wiki-records.jsonl")
    p.add_argument("--out", default=default_out, help="output JSONL path (default: %(default)s)")
    p.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="cap pages read from EACH source at N (see module docstring)",
    )
    p.add_argument(
        "--offline", metavar="DIR", default=None,
        help="read fixtures from DIR instead of the network (see read_offline_fixtures())",
    )
    p.add_argument(
        "--dump-path", metavar="PATH", default=None,
        help="reuse an already-downloaded Fandom .xml.7z dump instead of fetching it",
    )
    p.add_argument("--contact", default=None, help="override the User-Agent contact token")
    p.add_argument("-v", "--verbose", action="store_true", help="DEBUG-level logging")
    return p.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    """Placeholder orchestration -- filled in as the remaining pieces (dump reader, API walkers,
    mwparserfromhell rendering, sanitize, sort+emit, failure policy) land. Kept import-clean and
    argument-parsing-complete from the first commit so py_compile and --help both work throughout
    this task's history, per PLAN-v6.md's "commit each coherent piece as its own commit"."""
    _LOG.error("run(): not yet implemented")
    return 1


if __name__ == "__main__":
    sys.exit(main())
