#!/usr/bin/env python3
"""valheim-wiki-ingest.py -- offline ingestion of the Valheim Fandom and Weird Gloop wikis into
wiki-records.jsonl, the flat per-heading corpus task W2's index build reads. See PLAN-v6.md
(sections "W1 -- Ingestion" and "W6 -- Close the dropped-template gap") for the authoritative
spec; this docstring covers usage, not rationale.

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
     sections, rendering every DATA-BEARING template into readable "key: value" prose lines (not
     just ones named "infobox*" -- W6 generalized this after finding that {{drop table}},
     {{spawn table}}, {{loot table}} and inline {{Item link}} recipe/drop mentions were being
     silently dropped; see render_template()'s dispatch and the EXCLUDED_TEMPLATE_NAMES /
     ITEM_LINK_TEMPLATE_NAMES / ROW_TABLE_TEMPLATES constants for the survey backing each
     category), and rendering every wikitable into one self-describing line per row so a number
     never separates from the column label it belongs to (render_wikitable()).
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
  - The run exits non-zero ONLY on the hard-failure path: fewer pages were attempted than
    required_page_floor() says is plausible evidence of a real run (see its own comment -- this
    is what catches a run that fetched NOTHING, or next to nothing, which a skip-ratio check
    alone cannot: 0 skipped / 0 attempted is not "over 5%"), more than 5% of the pages it DID
    attempt failed to parse, or the output file could not be written. Either way it writes
    nothing -- a half-built (or empty) corpus is worse than a stale one, and this is the one case
    where stopping the downstream index build is exactly correct.
  - The final summary line is always emitted, at INFO on a clean run and WARNING when any page
    was skipped, and always includes the literal token "SUMMARY" so `journalctl -u
    valheim-wiki-refresh | grep SUMMARY` finds it -- a slowly-rising skip count across weekly runs
    is the only early warning that a wiki changed its template structure under us, and that is
    useless if a human has to read full-verbosity logs to notice it.

USAGE:
    python valheim-wiki-ingest.py [--out PATH] [--limit N] [--offline DIR] [--dump-path PATH]
                                   [--contact TEXT] [-v]

    --out PATH        Output path for the JSONL corpus. Default: wiki-records.jsonl under
                       $VALHEIM_WIKI_ROOT (default /var/lib/valheim-wiki), matching
                       valheim-wiki-index.py's own default -- see WIKI_ROOT below.
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
    --dump-path PATH   Reuse an already-DECOMPRESSED copy of the Fandom dump's XML (not the .7z
                       archive) instead of downloading and decompressing it again -- handy while
                       iterating locally. If PATH exists, it is read as-is and nothing is fetched;
                       if it does not exist, the dump is downloaded and decompressed straight to
                       PATH, so a second run with the same --dump-path reuses it. Meaningless with
                       --offline (which touches no dump file, decompressed or not).
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

# ---------------------------------------------------------------- evidence floor (C1 review fix)
# skip_ratio() alone cannot catch a run that fetched NOTHING: 0 skipped / 0 attempted is 0.0,
# comfortably under FAILURE_THRESHOLD, so a TOTAL fetch failure (every source silently returning
# zero pages -- see WikiClient.get()'s "error" key handling below for one concrete way that
# happens with no exception ever raised) sailed straight through the ratio check and
# os.replace()'d an EMPTY corpus over the last good one. That is strictly worse than doing
# nothing: a stale corpus still answers questions; an empty one makes the bot say "I don't know"
# forever, on a weekly unattended timer, while every signal (exit 0, INFO-level SUMMARY) reports
# success.
#
# The fix is a POSITIVE check -- did this run produce real EVIDENCE of success -- not another
# negative one (did we see enough failure). Inferring success from the absence of a failure
# signal is exactly the bug above. The floor is scaled to what the run itself declared it was
# trying to do:
#   - A genuine unrestricted run against the live wikis should see something in the neighborhood
#     of the real corpus: ~2,200 articles total (~1,034 Weird Gloop + Fandom's ~1,179, per
#     PLAN-v6.md's source table). MIN_LIVE_RUN_PAGES (500) sits comfortably below even the
#     SMALLER of the two wikis alone, so any healthy run -- including one where a whole source
#     temporarily degrades -- clears it with room to spare, while a run that silently fetched
#     nothing (or next to nothing) from every source never does.
#   - --limit and --offline exist specifically to fetch far fewer pages than that ON PURPOSE (see
#     their own --help text above -- exercising this script's parsing/failure-policy logic
#     without pulling the whole corpus, including in CI). Holding those to the live-run floor
#     would make the very flags built for testing this script unusable for testing this script.
#     They are held to a much lower floor instead: at least ONE page attempted -- exactly the
#     evidence the zero-fetch bug above was missing, without defeating either flag's own purpose.
MIN_LIVE_RUN_PAGES = 500
MIN_TEST_RUN_PAGES = 1


def required_page_floor(args: argparse.Namespace) -> int:
    """The minimum number of pages `run()` must have ATTEMPTED before it is allowed to write a
    corpus at all -- see the constants' comment above for why this number depends on whether the
    run declared itself a full live run or a deliberately-scoped --limit/--offline one."""
    return MIN_TEST_RUN_PAGES if (args.offline or args.limit is not None) else MIN_LIVE_RUN_PAGES

# Sections rendered from an infobox template ("infobox creature", "infobox armor", ...) are never
# the wrapper template itself -- that one only carries positional (label, content) pairs, no
# key/value data of its own. Matched by prefix, case-insensitively, on the template's own name.
INFOBOX_NAME_PREFIX = "infobox"
INFOBOX_TABBER_NAME = "infoboxtabber"

# ---------------------------------------------------------------- template classification (W6)
# W1 rendered ONLY templates whose name started with "infobox"; everything else fell through to
# strip_code(), which drops a template's content outright. This is the survey-backed
# classification that replaces that prefix check -- see this task's report for the full survey
# (19 real pages across creatures/items/food/crafting stations/biomes on both wikis, every
# top-level template counted). The three buckets below are checked in this order by
# render_template(): excluded (render as nothing) > item-link (one inline "Name" or "Name xN") >
# row-table wrapper (one line per nested row) > infobox/InfoboxTabber (existing render_infobox()) >
# generic (fall back to the same "key: value" prose treatment infoboxes get, so an unrecognized
# data template still surfaces its parameters instead of vanishing).
#
# EXCLUDED: presentational only -- navboxes, hatnotes, and the maintenance-banner families
# PLAN-v6.md names explicitly. Survey-CONFIRMED by fetching each template's own source (all five
# are either a Lua hatnote module or a #REDIRECT to a "*Nav" navbox template): "for", "creatures",
# "biomes", "weapons", "armor". The "*nav" suffix rule below generalizes to every navbox found in
# the survey (buildingnav, foodsnav, toolsnav, weaponsnav) AND to navboxes never sampled (a
# "*Nav" name is Fandom's own naming convention for this template family, not a guess specific to
# Valheim). "stub"/"cleanup"/"work in progress"/"wip"/"disambig" are NOT survey-confirmed (none of
# the 19 sampled pages carried one) but are added defensively because PLAN-v6.md names this exact
# category by name as noise to exclude; if that turns out wrong, deleting a line here is cheap.
EXCLUDED_TEMPLATE_NAMES = frozenset({
    "for", "creatures", "biomes", "weapons", "armor",  # survey-confirmed navbox/hatnote redirects
    "stub", "cleanup", "work in progress", "wip", "disambig", "disambiguation",  # plan-named, not sampled
})
EXCLUDED_TEMPLATE_SUFFIXES = ("nav",)  # e.g. buildingnav, foodsnav, toolsnav, weaponsnav (all 4 in survey)

# Survey-confirmed: 47 occurrences across 12/19 pages (by far the most common non-infobox
# template), used both standalone in bullet lists ("* {{Item link|Entrails|4}}") and inside
# wikitable cells (Forge's recipe-cost table). Renders inline as "Name" or "Name xN" -- see
# render_item_link().
ITEM_LINK_TEMPLATE_NAMES = frozenset({"item link"})

# Survey-confirmed: the dominant "creature drops" / "spawn conditions" / "biome loot chance"
# shape, found on every creature and biome page sampled (6 "drop table", 5 "spawn table", 4 "loot
# table" -- 15 of 19 pages carried at least one). All three share one wikitext shape:
# `{{X table|{{X row|k=v|...}}{{X row|k=v|...}}}}` -- a wrapper template with a single unnamed
# param whose value is several sibling "X row" calls concatenated with only whitespace between
# them, each row a flat set of named params. See render_row_table().
ROW_TABLE_TEMPLATES = {
    "drop table": "drop row",
    "spawn table": "spawn row",
    "loot table": "loot row",
}

# ---------------------------------------------------------------- output location (C2 review fix)
# Matches valheim-wiki-index.py's own convention exactly (see that script's module docstring:
# "All paths take an env override (VALHEIM_WIKI_ROOT) ... so this can run against fixtures
# without touching /var") so the two scripts agree on where the corpus lives without either one
# having to know about the other's argv -- a bare `python valheim-wiki-ingest.py` and a bare
# `python valheim-wiki-index.py`, run by the same operator in the same shell with no flags, now
# land in the same place by default.
#
# Previously this script's own --out default was the SCRIPT'S OWN directory
# (os.path.dirname(__file__)) -- harmless run from a checkout, but install-dashboard.sh installs
# this script to /usr/local/sbin/, and valheim-wiki-refresh.service's ProtectSystem=strict makes
# /usr read-only except for ReadWritePaths=/var/lib/valheim-wiki. So the unit's first (unattended,
# operator-supervised-in-theory) run raised PermissionError on its very first write, run()
# returned 1, and the non-dash-prefixed second ExecStart line -- the index build -- never ran, no
# matter how many times the timer fired. See this task's report (C2) for the full chain.
WIKI_ROOT = os.environ.get("VALHEIM_WIKI_ROOT", "/var/lib/valheim-wiki").rstrip("/")

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
    through every function separately.

    H2 review fix: record_skip() is the ONE place a dropped page is accounted for, called both by
    raw_pages_to_records() (a page whose wikitext failed to parse) and by walk_allpages() (a page
    the fetch layer itself never got usable content for -- missing/invalid/no revisions/no
    content, see that function's own docstring). Routing both through the same method is what
    makes skip_ratio() -- and therefore the 5% hard-failure threshold -- see fetch-layer drops at
    all; before this fix those were bare `continue`s that never touched `attempted` or `skipped`,
    so a MediaWiki response-shape change could silently shrink the corpus while this same SUMMARY
    line still reported "0 skipped"."""

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
        """Count one dropped page, whatever stage dropped it -- a parse failure
        (raw_pages_to_records()) or a fetch-layer drop (walk_allpages(): missing/invalid/no
        revisions/no content). Always logs at WARNING with the page's title, per this script's
        FAILURE POLICY: a slowly-rising skip count is the only early warning that a wiki changed
        shape under us, and that is useless if it is not in the journal."""
        self.attempted += 1
        self.skipped_titles.append(title)
        _LOG.warning("skip: page %r -- %s", title, reason)


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

    @property
    def session(self):
        """Exposed (rather than left as `_session`) for download_fandom_dump(): a plain S3 file
        GET reuses this client's session for its User-Agent, but is not a MediaWiki API call, so
        it goes around WikiClient.get() entirely (no maxlag param, no JSON/maxlag-error decoding)
        rather than awkwardly overloading that method for a request shape it was not written for."""
        return self._session

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
        MAX_RETRIES exhausted -- callers decide whether that page counts as a skip.

        Also raises RuntimeError IMMEDIATELY (no retry) on any OTHER MediaWiki API error body
        (HTTP 200 with `{"error": {...}}`, code != "maxlag" -- e.g. readapidenied, or a
        deprecated/removed parameter after an API version bump): retrying a malformed or
        forbidden request cannot succeed, and letting it through as an ordinary-looking response
        is exactly how a caller like walk_allpages() silently turns "the API refused this
        request" into "this wiki has zero pages" with no exception anywhere to catch."""
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
            if error:
                # Any OTHER MediaWiki error (readapidenied, a deprecated/removed param after an
                # API version bump, etc.) comes back as HTTP 200 with an {"error": {...}} body --
                # raise_for_status() never fires for this. Left unhandled, walk_allpages()'s own
                # `(data.get("query") or {}).get("pages") or []` would quietly turn this into ZERO
                # pages, indistinguishable from "this wiki really has no more pages" -- exactly
                # the silent-empty-result shape C1 exists to catch, but at the source instead of
                # after the fact. Retrying will not help (the request is malformed/forbidden, not
                # transient), so this fails hard immediately rather than burning MAX_RETRIES.
                _LOG.error(
                    "%s returned a MediaWiki API error (code=%r): %s",
                    self.base_url, error.get("code"), error.get("info") or error,
                )
                raise RuntimeError(
                    f"{self.base_url}: MediaWiki API error {error.get('code')!r}: "
                    f"{error.get('info') or error!r}"
                )
            return data

        raise last_exc or RuntimeError(
            f"{self.base_url}: exhausted {MAX_RETRIES} retries with no successful response"
        )


# ---------------------------------------------------------------- mwparserfromhell rendering
# Everything in this section takes parsed mwparserfromhell Wikicode, never a raw string -- the
# one regex in the whole section (_WHITESPACE_RE) only collapses whitespace in already-rendered
# plain text, never reads a template parameter or a number out of wikitext. See PLAN-v6.md's
# warning that regex-over-wikitext is "the single failure mode most likely to make this whole
# feature quietly useless" (it silently yields the template CALL where you expect a number).
_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")
_MENTION_RE = re.compile(r"@(everyone|here)", re.IGNORECASE)
# A Private Use Area code point stands in for <br> while strip_code() runs, because strip_code()'s
# own collapse=True (the default, and there is no good reason to turn it off) eats plain "; " --
# confirmed empirically while building this script: replacing a <br> tag with the literal string
# "; " survives code.replace() but strip_code() then collapses it right back down to a single
# space, silently undoing the separator and running list-style infobox values together with no
# punctuation at all (e.g. Leather Armor's "materials 2 = 6 [[Deer hide]]<br>5 [[Bone fragments]]"
# rendering as "6 Deer hide 5 Bone fragments" instead of "...hide; 5 Bone..."). A PUA character is
# never legitimate wikitext content, so this is a safe, unambiguous marker that strip_code() has
# no punctuation-collapsing rule for.
_BR_PLACEHOLDER = ""
_BR_RUN_RE = re.compile(_BR_PLACEHOLDER + r"+")
_STRAY_SEPARATOR_RE = re.compile(r"^\s*;\s*|\s*;\s*$", re.MULTILINE)
# Some source wikitext already puts its own ", " before a <br> as the author's own list
# separator (confirmed on Resistance's "Source for players" table: "[[X]] (Y),<br>[[Z]] (W)") --
# combined with the <br>-> "; " substitution above, that doubles up into "Y),; Z" instead of the
# intended "Y); Z". This collapses any run of 2+ adjacent comma/semicolon separators (with
# whitespace between them) down to one "; ", regardless of which one is the placeholder-derived
# semicolon and which is the source's own comma.
_DOUBLE_SEPARATOR_RE = re.compile(r"[,;]\s*[,;]\s*")


def clean_wikicode(code) -> str:
    """Render a parsed mwparserfromhell Wikicode fragment (a whole page, one section, one
    infobox/template parameter's value, or one wikitable cell -- this function is used
    recursively at every one of those levels) down to plain prose, per PLAN-v6.md step 6's
    sanitize list PLUS the W6 template/table rendering it composes with:

      - <ref> tags and HTML comments are removed outright.
      - <br> tags become "; " (via a placeholder, see _BR_PLACEHOLDER's comment) so list-style
        values stay readable instead of running together with no separator.
      - File/Image/Category wikilinks are removed outright (not just their brackets --
        strip_code() alone leaves a File: link's caption text and a bare "Category:Foo" behind as
        visible prose); every other wikilink reduces to its display text.
      - Every wikitable (a `table` Tag node -- mwparserfromhell has no dedicated Table node class,
        see render_wikitable()'s docstring) is replaced with its rendered "one self-describing
        line per row" prose BEFORE strip_code() runs, so a table never reaches strip_code() as
        raw markup for it to flatten into an unlabelled run of cells.
      - Every TOP-LEVEL template (recursive=False -- nested templates, e.g. a "drop row" inside
        its "drop table" wrapper or an "Item link" inside a wikitable cell, are handled by the
        recursive clean_wikicode() calls render_template() and render_wikitable() make on their
        own nested content, not by this outer scan) is replaced with render_template()'s result
        BEFORE strip_code() runs, per W6: strip_code() on its own drops a template's content
        entirely, which is exactly the bug this task exists to fix.
      - Finally, whitespace collapses and any literal @everyone/@here is defused, mirroring
        valheim-bot.py's own EVERYONE_RE backstop even though this text never reaches that bot's
        system prompt as anything but clearly-labelled reference data (see PLAN-v6.md "The two
        rules that make this safe").

    Never raises on malformed input: mwparserfromhell's own parse is forgiving, and the loops here
    only ever remove or replace nodes the parser already found -- there is no path that reads a
    node this function did not itself enumerate. A replace()/remove() call whose target node was
    already consumed as part of a larger replacement earlier in the same pass raises ValueError,
    which every loop below catches and ignores rather than letting propagate -- see
    raw_pages_to_records() for why an actual parse failure (not this) still needs to surface as an
    exception rather than being swallowed here too."""
    for tag in list(code.filter_tags(recursive=True)):
        tag_name = str(tag.tag).strip().lower()
        if tag_name == "ref":
            try:
                code.remove(tag)
            except ValueError:
                pass  # already removed as part of a larger node (e.g. an enclosing template)
        elif tag_name in ("br", "br/"):
            try:
                code.replace(tag, _BR_PLACEHOLDER)
            except ValueError:
                pass
        elif tag_name == "table":
            try:
                code.replace(tag, render_wikitable(tag))
            except ValueError:
                pass  # a table nested inside another table's cell -- already rendered by then

    for link in list(code.filter_wikilinks(recursive=True)):
        title = str(link.title).strip().lower()
        if title.startswith(("file:", "image:", "category:")):
            try:
                code.remove(link)
            except ValueError:
                pass

    for template in list(code.filter_templates(recursive=False)):
        try:
            code.replace(template, render_template(template))
        except ValueError:
            pass  # already consumed as part of a larger replacement earlier in this same pass

    text = code.strip_code(normalize=True, collapse=True)
    text = _BR_RUN_RE.sub("; ", text)  # see _BR_PLACEHOLDER's comment above
    text = _DOUBLE_SEPARATOR_RE.sub("; ", text)  # e.g. source's own ",<br>" doubling up with the above
    text = _STRAY_SEPARATOR_RE.sub("", text)  # a <br> at the very start/end of a value/line
    text = _MENTION_RE.sub(r"\1", text)
    text = _WHITESPACE_RE.sub(" ", text)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def render_infobox(template, tab_label: Optional[str] = None) -> str:
    """Render one infobox template's parameters as readable `key: value` prose lines -- PLAN-v6.md
    step 4: "the model reads prose, not wikitext." `template` is an mwparserfromhell Template node
    whose name starts with "infobox" (see INFOBOX_NAME_PREFIX). `tab_label` is the tab this
    infobox came from when it was nested inside {{InfoboxTabber}} (e.g. "Head" for Leather Armor's
    helmet tab) -- PLAN-v6.md step 4's own example is "Protector Armor, Cast Helmet" staying
    distinguishable, which this satisfies by heading the block with both the set/page title (the
    infobox's own `title` param, falling back to the page context if absent) and the tab label.

    Every parameter is emitted verbatim as `name: value`, including star-suffixed names like
    "health 0star" and "damage 1star" (Boar-style creatures put per-level stats directly in the
    parameter name rather than nesting a tab per level) -- this script does not attempt to
    re-derive a friendlier key name, since the raw wiki parameter name is already the kind of
    short, stable, greppable label a search index and a model both handle fine, and inventing a
    renaming table is exactly the kind of scope this plan does not ask for."""
    own_title = None
    if template.has("title"):
        own_title = clean_wikicode(template.get("title").value).strip() or None
    header_bits = [b for b in (own_title, tab_label) if b]
    header = "INFOBOX: " + " -- ".join(header_bits) if header_bits else "INFOBOX"

    lines = [header]
    for param in template.params:
        name = str(param.name).strip()
        if not name or name == "title":
            continue  # already used in the header above
        value = clean_wikicode(param.value).strip()
        if not value:
            continue  # PLAN-v6.md's example infoboxes leave many optional fields blank
        lines.append(f"{name}: {value}")
    return "\n".join(lines)


def render_infobox_tabber(template) -> str:
    """Render `{{InfoboxTabber|Label1|{{infobox ...}}|Label2|{{infobox ...}}|...}}` (e.g. Leather
    Armor's four armor pieces). InfoboxTabber's own params are positional label/content pairs
    (confirmed against real Fandom wikitext: params "1","2","3","4" hold "Head", the helmet's
    {{infobox armor}}, "Chest", the tunic's {{infobox armor}}, ...); each content slot is
    re-parsed for its nested infobox template(s) and rendered via render_infobox() with its label
    from the immediately preceding positional slot, so e.g. "Leather helmet -- Head" and "Leather
    tunic -- Chest" stay distinguishable per PLAN-v6.md's own example ("Protector Armor, Cast
    Helmet")."""
    blocks = []
    params = template.params
    for i in range(0, len(params) - 1, 2):
        label = clean_wikicode(params[i].value).strip()
        nested_code = mwparserfromhell.parse(str(params[i + 1].value))
        for nested in nested_code.filter_templates(recursive=False):
            nested_name = str(nested.name).strip().lower()
            if nested_name.startswith(INFOBOX_NAME_PREFIX):
                blocks.append(render_infobox(nested, tab_label=label or None))
    return "\n\n".join(blocks)


def render_item_link(template) -> str:
    """{{Item link|Name}} or {{Item link|Name|amount}} (amount may also be the named param
    `amount`/`qty` rather than positional) -- survey-confirmed the most common non-infobox
    template on Valheim wiki pages (47 occurrences across 12 of 19 sampled pages), used both
    standalone in bullet-list recipes ("* {{Item link|Entrails|4}}") and inside wikitable cells
    (Forge's recipe-cost table). Renders as "Name" or "Name xN". Extra DISPLAY-only params
    (`size=`, `nolink=`, seen on Leather Armor's wikitable icons) are ignored -- they only affect
    the wiki's own icon/link styling, never mechanics, and are told apart from a positional amount
    by name: a positional amount's mwparserfromhell param name is the literal digit "2" (its
    position), while a named display param's name is a word."""
    params = template.params
    if not params:
        return ""
    name = clean_wikicode(params[0].value).strip()
    amount = None
    if template.has("amount"):
        amount = clean_wikicode(template.get("amount").value).strip()
    elif template.has("qty"):
        amount = clean_wikicode(template.get("qty").value).strip()
    elif len(params) > 1 and str(params[1].name).strip() == "2":
        candidate = clean_wikicode(params[1].value).strip()
        if candidate:
            amount = candidate
    return f"{name} x{amount}" if amount else name


def render_row_table(template, row_template_name: str) -> str:
    """Render a `{{X table|{{X row|k=v|...}}{{X row|k=v|...}}}}` wrapper -- survey-confirmed the
    dominant "creature drops" / "spawn conditions" / "biome loot chance" shape (drop table/spawn
    table/loot table, see ROW_TABLE_TEMPLATES; found on 15 of 19 sampled pages) -- as one line per
    row, each row's own params rendered as "key: value, key: value" so a model reads a
    self-contained fact per line (e.g. "item: Boar trophy, 0star: 15%, 1star: 15%, 2star: 15%")
    instead of the nested blob strip_code() would otherwise erase completely. This is what puts a
    creature's drops in its own record, attached to that creature, per PLAN-v6.md's W6 check.

    `template`'s unnamed param value is a concatenation of several sibling `row_template_name`
    calls with only whitespace between them (confirmed identical across drop table/spawn
    table/loot table on real pages) -- re-parsed fresh as its own Wikicode so filter_templates()
    can enumerate them; anything inside that value which is NOT a `row_template_name` call is
    ignored rather than guessed at (lenient, not silently wrong)."""
    label = str(template.name).strip().title()
    lines = []
    for param in template.params:
        nested_code = mwparserfromhell.parse(str(param.value))
        for row in nested_code.filter_templates(recursive=False):
            if str(row.name).strip().lower() != row_template_name:
                continue
            bits = []
            for p in row.params:
                pname = str(p.name).strip()
                pval = clean_wikicode(p.value).strip()
                if pname and pval:
                    bits.append(f"{pname}: {pval}")
            if bits:
                lines.append("- " + ", ".join(bits))
    return label + ":\n" + "\n".join(lines) if lines else ""


def render_generic_template(template) -> str:
    """Fallback for any data-bearing template that is not one of the specifically-recognized
    shapes above -- PLAN-v6.md W6's "render data-bearing templates generally rather than by an
    infobox name prefix." Same "key: value" prose treatment render_infobox() already gives
    infoboxes, so a template this script has not seen before still surfaces its parameters
    (readable, if not beautifully labelled) instead of vanishing the way strip_code() alone would
    drop it. A purely positional param whose value contains no other templates/links and is a
    bare short token (e.g. {{cols|2|...}}'s leading "2", a column-count layout hint) is skipped --
    that heuristic accepts the small risk of dropping a genuinely meaningful bare number in
    exchange for not littering every generic-rendered template with layout noise; a param whose
    value has any real content (prose, a link, a nested template) is never skipped by it."""
    lines = [str(template.name).strip() + ":"]
    for param in template.params:
        name = str(param.name).strip()
        value_code = param.value
        is_positional = name.isdigit()
        has_structure = bool(
            value_code.filter_templates(recursive=False) or value_code.filter_wikilinks(recursive=False)
        )
        raw_len = len(str(value_code).strip())
        if is_positional and not has_structure and raw_len <= 4:
            continue  # bare short positional value, e.g. a layout hint -- see docstring
        value = clean_wikicode(value_code).strip()
        if value:
            lines.append(f"{name}: {value}")
    return "\n".join(lines) if len(lines) > 1 else ""


def render_template(template) -> str:
    """Dispatch for every TOP-LEVEL template clean_wikicode() finds (see its own docstring for why
    this is recursive=False and how nested templates still get handled). Checked in this order:
    excluded (presentational, render as nothing) -> item-link (one inline mention) -> row-table
    wrapper (drop/spawn/loot table) -> infobox/InfoboxTabber -> generic key:value fallback. See
    the EXCLUDED_TEMPLATE_NAMES/ITEM_LINK_TEMPLATE_NAMES/ROW_TABLE_TEMPLATES comments above this
    module's constants for the survey backing each bucket."""
    name = str(template.name).strip()
    key = name.lower()
    if key in EXCLUDED_TEMPLATE_NAMES or key.endswith(EXCLUDED_TEMPLATE_SUFFIXES):
        return ""
    if key in ITEM_LINK_TEMPLATE_NAMES:
        return render_item_link(template)
    if key in ROW_TABLE_TEMPLATES:
        return render_row_table(template, ROW_TABLE_TEMPLATES[key])
    if key == INFOBOX_TABBER_NAME:
        return render_infobox_tabber(template)
    if key.startswith(INFOBOX_NAME_PREFIX):
        return render_infobox(template)
    return render_generic_template(template)


def render_wikitable(table_tag) -> str:
    """Render one wikitable into one self-describing line per data row: "Header1: cell1,
    Header2: cell2, ...". PLAN-v6.md W6: "a number without its label is worse than no number,
    because it still reads as authoritative" -- this is what keeps e.g. Resistance's "200%"
    attached to the damage type it applies to instead of floating free in run-on text.

    mwparserfromhell has NO dedicated Table node class (confirmed while building this script,
    version 0.7.2): a `{| ... |}` wikitable parses as a single Tag node with `tag == "table"`,
    and its rows/cells parse as further Tag nodes (`tr`, `th`, `td`) nested inside it -- so
    `table_tag` here is a Tag, walked via its own `.contents`, not a specialized table API.

    Header labels come from the run of top-level `th` cells before any `tr` (the common shape:
    a table's header row is often written without a leading `|-`, so those `th` cells sit as
    direct siblings of the `tr` rows rather than inside one) -- confirmed against Resistance's own
    "Source for players" table and Forge's recipe-cost table. A LATER `tr` made up entirely of
    `th` cells is treated as a header row too (a table can redefine its columns partway through),
    replacing the current header set for rows after it.

    Column alignment is POSITIONAL (header index N labels data-cell index N) and does NOT account
    for colspan/rowspan -- a cell's own colspan/rowspan attribute is not inspected. KNOWN, STATED
    GAP (PLAN-v6.md W6 explicitly allows this): most Valheim wiki data tables (resistances, food
    stats, drop tables rendered as tables) are simple colspan-free grids in practice, and exact
    colspan/rowspan-aware alignment is a materially larger effort for a corpus that already gets
    the common case's numbers correctly labelled. When a row's cell count does not match the
    current header count, generic "Column N: value" labels are used instead of mis-attributing a
    header to the wrong cell -- a wrong label would be worse than a generic one."""
    headers = []
    lines = []
    for child in table_tag.contents.filter_tags(recursive=False):
        tag_name = str(child.tag).strip().lower()
        if tag_name == "th":
            label = clean_wikicode(child.contents).strip()
            if label:
                headers.append(label)
            continue
        if tag_name != "tr":
            continue  # e.g. "caption" -- not a data row, nothing to attach a label to
        cells = [
            c for c in child.contents.filter_tags(recursive=False)
            if str(c.tag).strip().lower() in ("td", "th")
        ]
        if cells and all(str(c.tag).strip().lower() == "th" for c in cells):
            headers = [clean_wikicode(c.contents).strip() for c in cells]
            continue
        values = [clean_wikicode(c.contents).strip() for c in cells]
        if not any(values):
            continue
        if headers and len(headers) == len(values):
            bits = [f"{h}: {v}" for h, v in zip(headers, values) if v]
        else:
            bits = [f"Column {i + 1}: {v}" for i, v in enumerate(values) if v]
        if bits:
            lines.append("- " + ", ".join(bits))
    return "\n".join(lines)


def heading_and_body(section):
    """Split one mwparserfromhell get_sections() result into (heading_text, body_wikicode).
    heading_text is "" for the lead section (the part of the page before its first heading).

    get_sections(include_headings=True) puts the Heading node itself as one of the section's own
    nodes, so calling clean_wikicode() on the whole section -- as an early version of this script
    did -- renders the heading's own words as the FIRST line of the section's body text too (e.g.
    a "Gore" heading over one paragraph produced body text starting "Gore\\nSwings its head..."),
    and worse, a heading whose only content is a template strip_code() drops entirely (Boar's
    "Drops"/"Spawning" sections are 100% `{{drop table}}`/`{{spawn table}}` calls) then renders as
    body text that is JUST the heading word repeated -- a record whose "text" field says "Drops"
    and nothing else, which is worse than no record at all. This function removes the heading
    node from the body before rendering it, so such a section's body text is correctly empty and
    page_to_sections() drops the record rather than keeping a content-free one."""
    headings = section.filter_headings()
    if not headings:
        return "", section
    heading_node = headings[0]
    heading_text = clean_wikicode(mwparserfromhell.parse(str(heading_node.title))).strip()
    remaining_wikitext = "".join(str(n) for n in section.nodes if n is not heading_node)
    return heading_text, mwparserfromhell.parse(remaining_wikitext)


def page_to_sections(title: str, wikitext: str) -> list:
    """Parse one page's wikitext into (heading, text) pairs, per PLAN-v6.md steps 4-5. Every
    section -- including the lead (heading "") -- is rendered by the same clean_wikicode() call,
    which (as of W6) renders infoboxes, InfoboxTabber, drop/spawn/loot tables, Item link mentions
    and wikitables all IN PLACE, wherever they actually sit in the page's own wikitext, rather
    than the lead section needing special handling to collect and prepend an infobox rendering
    separately (W1's original design): a page's infobox is, in every real Valheim wiki page
    sampled, the very first thing in the lead anyway, so rendering it in place already produces
    the same "infobox first" reading order as the old explicit-prepend did, with one fewer
    special case.

    Sections whose rendered text is empty after sanitizing (e.g. a "Gallery" heading whose only
    content was <gallery> image markup) are dropped -- an empty record is not reference material
    for anything, and W2's search() has nothing to rank it against.

    Raises whatever mwparserfromhell.parse()/get_sections() raises on genuinely malformed input;
    callers (see raw_pages_to_records()) are responsible for turning that into a logged skip, per
    this script's failure policy -- this function itself makes no attempt to recover from a bad
    parse, so a caller can tell "this page parsed to nothing" (empty list, not a failure) apart
    from "this page could not be parsed at all" (an exception)."""
    code = mwparserfromhell.parse(wikitext)
    sections = code.get_sections(flat=True, include_lead=True, include_headings=True)
    out = []
    for section in sections:
        heading, body_code = heading_and_body(section)
        text = clean_wikicode(body_code)
        if text:
            out.append((heading, text))
    return out


# ---------------------------------------------------------------- Fandom bulk dump (PLAN-v6.md step 1)
def _local_tag(tag: str) -> str:
    """Strip an ElementTree tag's namespace prefix ("{uri}name" -> "name"). MediaWiki's export
    schema version (currently 0.11, "http://www.mediawiki.org/xml/export-0.11/", confirmed against
    the real published dump while building this script) is deliberately never hardcoded here, so
    a future export-version bump does not silently break parsing the way matching the literal
    namespaced tag string would."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def iter_dump_pages(path: str, limit: Optional[int] = None) -> Iterator[RawPage]:
    """Stream-parse a decompressed MediaWiki export XML file (the Fandom bulk dump) via
    ElementTree.iterparse, yielding one RawPage per ns0 page and clearing each <page> element as
    soon as it is consumed so memory use stays bounded by roughly one page at a time rather than
    the whole file (the current dump decompresses to ~9 MB, comfortably fine either way, but a
    future larger dump should not require rewriting this).

    A "pages_current" dump element can, per the export schema, carry more than one <revision> --
    in practice this dump has exactly one, but if that ever changes the LAST <revision> under the
    <page> is used, since that is the one "current" describes."""
    count = 0
    for _, elem in ET.iterparse(path, events=("end",)):
        if _local_tag(elem.tag) != "page":
            continue
        try:
            title = None
            ns_text = None
            revisions = []
            for child in elem:
                local = _local_tag(child.tag)
                if local == "title":
                    title = child.text or ""
                elif local == "ns":
                    ns_text = child.text
                elif local == "revision":
                    revisions.append(child)
            if (ns_text or "").strip() != "0" or not title or not revisions:
                continue
            revision = revisions[-1]
            revid, timestamp, wikitext = 0, "", None
            for rchild in revision:
                rlocal = _local_tag(rchild.tag)
                if rlocal == "id":
                    revid = int(rchild.text) if rchild.text and rchild.text.strip().isdigit() else 0
                elif rlocal == "timestamp":
                    timestamp = rchild.text or ""
                elif rlocal == "text":
                    wikitext = rchild.text or ""
            if wikitext is None:
                continue
            yield RawPage(title=title, wikitext=wikitext, revid=revid, timestamp=timestamp, source="fandom")
            count += 1
            if limit is not None and count >= limit:
                return
        finally:
            elem.clear()


def download_fandom_dump(client: "WikiClient", dest_path: str):
    """Fetch the published Fandom bulk dump (a plain S3 GET, not the MediaWiki API -- no maxlag,
    no query params, just a descriptive User-Agent) and decompress it in place with py7zr. Raises
    on any failure (network, non-200, extraction) -- callers treat a failure here as a hard
    failure for the whole run, not a per-page skip, since without the base corpus there is nothing
    sensible to fall back to for ~1,000 Fandom pages. `client` is reused only for its session
    (User-Agent) and is not passed maxlag/serialization params here since this is a plain file
    download, not a MediaWiki API call."""
    if requests is None:
        raise RuntimeError("the 'requests' package is not installed -- see requirements-ingest.txt")
    if py7zr is None:
        raise RuntimeError("the 'py7zr' package is not installed -- see requirements-ingest.txt")
    resp = client.session.get(FANDOM_DUMP_URL, timeout=REQUEST_TIMEOUT_S * 4, stream=True)
    resp.raise_for_status()
    archive_path = dest_path + ".7z"
    with open(archive_path, "wb") as fh:
        for chunk in resp.iter_content(chunk_size=1 << 16):
            fh.write(chunk)
    extract_dir = dest_path + ".extracted"
    with py7zr.SevenZipFile(archive_path, mode="r") as archive:
        names = archive.getnames()
        if len(names) != 1:
            _LOG.warning("dump archive contains %d files, expected 1: %r", len(names), names)
        archive.extractall(path=extract_dir)
    extracted_name = names[0]
    os.replace(os.path.join(extract_dir, extracted_name), dest_path)


# ---------------------------------------------------------------- MediaWiki API walk (steps 2-3)
def _revision_content(rev: dict) -> Optional[str]:
    """A revision's wikitext, tolerating both the classic `rev["content"]` shape (confirmed
    against both live wikis while building this script -- neither requires `rvslots` to return
    content this way) and the newer MediaWiki "slots" shape (`rev["slots"]["main"]["content"]`),
    in case either wiki's software version ever changes which one it returns by default."""
    if "content" in rev:
        return rev["content"]
    slots = rev.get("slots") or {}
    main = slots.get("main") or {}
    return main.get("content")


def walk_allpages(
    client: "WikiClient", source: str, stats: "RunStats", limit: Optional[int] = None
) -> Iterator[RawPage]:
    """Batched, continuation-following walk over a wiki's live ns0 pages: `action=query&
    generator=allpages&gapnamespace=0&prop=revisions&rvprop=content|ids|timestamp`, GAP_LIMIT
    (500) pages per request. PLAN-v6.md specifies exactly this mechanism for BOTH the Fandom delta
    walk (step 2) and the full Weird Gloop walk (step 3), so this one function serves both call
    sites -- see ingest_fandom_delta() and the main Weird Gloop path in run_ingest() below for how
    each uses what comes out.

    Stops as soon as `limit` pages have been yielded (no further request is issued), regardless of
    how many more `continue` batches the wiki still has -- this is what makes `--limit 20` cheap
    against a live wiki with ~1,000+ pages rather than merely capping how much of a full walk gets
    kept.

    H2 review fix: a page the API reports as `missing`/`invalid`, one with no revisions, or a
    revision with no readable content, used to be a bare `continue` -- not logged, not counted in
    `stats`, so it was invisible to both the journal and skip_ratio()'s 5% failure threshold. A
    MediaWiki response-shape change could then silently shrink the corpus while the SUMMARY line
    still reported "0 skipped". Every one of those drops now goes through `stats.record_skip()`
    -- the SAME accounting a parse failure gets in raw_pages_to_records() -- so it is counted in
    `attempted`, counted in `skipped`, feeds skip_ratio() exactly like a parse failure would, and
    is logged at WARNING with its title (record_skip() already does this logging; see its own
    docstring)."""
    params = {
        "action": "query",
        "generator": "allpages",
        "gapnamespace": 0,
        "gaplimit": GAP_LIMIT,
        "prop": "revisions",
        "rvprop": "content|ids|timestamp",
        "format": "json",
        "formatversion": 2,
    }
    count = 0
    continue_params = {}
    while True:
        data = client.get({**params, **continue_params})
        pages = (data.get("query") or {}).get("pages") or []
        for page in pages:
            title = page.get("title") or "<untitled>"
            if page.get("missing"):
                stats.record_skip(title, "API reported this page as missing")
                continue
            if page.get("invalid"):
                stats.record_skip(title, "API reported this page as invalid")
                continue
            revisions = page.get("revisions") or []
            if not revisions:
                stats.record_skip(title, "no revisions returned for this page")
                continue
            rev = revisions[0]
            content = _revision_content(rev)
            if content is None:
                stats.record_skip(title, "revision had no readable content (content/slots.main.content)")
                continue
            yield RawPage(
                title=page.get("title", ""),
                wikitext=content,
                revid=rev.get("revid", 0) or 0,
                timestamp=rev.get("timestamp", ""),
                source=source,
            )
            count += 1
            if limit is not None and count >= limit:
                return
        if "continue" not in data:
            return
        continue_params = data["continue"]


def ingest_fandom_delta(
    client: "WikiClient", dump_timestamps: dict, stats: "RunStats", limit: Optional[int] = None
) -> Iterator[RawPage]:
    """PLAN-v6.md step 2: walk Fandom's live pages and yield only the ones worth overriding the
    dump's copy of -- a title whose live revision timestamp is strictly newer than what the dump
    recorded FOR THAT EXACT TITLE, or a title the dump did not have at all (page created after the
    dump was generated). Compared per-title rather than against one global "the dump's timestamp"
    cutoff, since the dump can (and does) contain pages last edited on many different dates; a
    single global cutoff would either re-fetch far more of the wiki than actually changed (if
    taken as the oldest page's timestamp) or silently miss real changes to older, rarely-edited
    pages (if taken as the newest). ISO-8601 "YYYY-MM-DDTHH:MM:SSZ" strings compare correctly with
    plain `<=`/`>`, so no date parsing is needed here."""
    for page in walk_allpages(client, "fandom", stats, limit=limit):
        dump_ts = dump_timestamps.get(page.title)
        if dump_ts is not None and page.timestamp <= dump_ts:
            continue
        yield page


# ---------------------------------------------------------------- offline fixtures (no network)
def read_offline_fixtures(fixture_dir: str) -> "tuple[list, list]":
    """Read canned pages from `fixture_dir` instead of touching the network at all -- no dump
    download, no decompression, no HTTP request of any kind. Exists so this script's actual
    behavior (mwparserfromhell rendering, sanitizing, sorting, the failure policy) can be verified
    without network access; see this task's report for the exact fixtures used.

    Expected layout (either file may be absent, which yields an empty list for that source -- an
    empty source is not an error, exactly like an empty `search()` result is not one for W2):

        fixture_dir/fandom.json      JSON list of {"title", "wikitext", "revid", "timestamp"}
        fixture_dir/weirdgloop.json  same shape

    This is deliberately simpler than reproducing the dump/delta reconciliation dance: offline
    verification is about proving the PARSING and ORCHESTRATION logic downstream of "here are some
    (title, wikitext, revid, timestamp) tuples," not about re-testing network mechanics that, by
    definition, this mode does not exercise."""

    def _load(name):
        path = os.path.join(fixture_dir, name)
        if not os.path.isfile(path):
            return []
        with open(path, "r", encoding="utf-8") as fh:
            raw_list = json.load(fh)
        return raw_list

    return _load("fandom.json"), _load("weirdgloop.json")


# ---------------------------------------------------------------- assembling records (steps 4-7)
def page_url(source: str, title: str) -> str:
    base = FANDOM_BASE_URL if source == "fandom" else WEIRDGLOOP_BASE_URL
    return base + urllib.parse.quote(title.replace(" ", "_"))


def raw_pages_to_records(raw_pages: Iterator[RawPage], stats: RunStats) -> Iterator[Record]:
    """PLAN-v6.md steps 4-6 applied to a stream of RawPage: parse with mwparserfromhell, split
    into sections, sanitize -- all inside page_to_sections()/clean_wikicode() above. A page whose
    parse raises ANY exception is logged with its title and skipped (RunStats.record_skip()),
    never silently dropped and never allowed to abort the whole run; PLAN-v6.md's failure policy
    (module docstring) is what decides, at the end of the run, whether the accumulated skip count
    is small enough to still write a corpus."""
    for raw in raw_pages:
        try:
            sections = page_to_sections(raw.title, raw.wikitext)
        except Exception as exc:  # noqa: BLE001 -- deliberately broad, see docstring
            stats.record_skip(raw.title, f"failed to parse -- {exc!r}")
            continue
        stats.record_success()
        url = page_url(raw.source, raw.title)
        for heading, text in sections:
            yield Record(
                title=raw.title, heading=heading, text=text, source=raw.source,
                revid=raw.revid, timestamp=raw.timestamp, url=url,
            )


def write_corpus(records: list, out_path: str):
    """PLAN-v6.md step 7: one JSON object per line, sorted by (title, heading, source) for a
    byte-stable re-run. Written to a temp file then os.replace()'d into place -- the same
    atomic-swap pattern task W2 uses for wiki.db, so a reader (or a second, concurrent ingest run)
    never sees a half-written corpus file."""
    records = sorted(records, key=lambda r: r.sort_key())
    tmp_path = out_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as fh:
        for record in records:
            fh.write(record.to_json_line())
            fh.write("\n")
    os.replace(tmp_path, out_path)
    return len(records)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if args.selftest:
        if mwparserfromhell is None:
            _LOG.error("the 'mwparserfromhell' package is not installed -- see requirements-ingest.txt")
            return 1
        return selftest()
    return run(args)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Ingest the Valheim Fandom + Weird Gloop wikis into wiki-records.jsonl.",
    )
    default_out = os.path.join(WIKI_ROOT, "wiki-records.jsonl")
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
        help="path to cache the decompressed Fandom dump XML at, or reuse it from if present",
    )
    p.add_argument("--contact", default=None, help="override the User-Agent contact token")
    p.add_argument("-v", "--verbose", action="store_true", help="DEBUG-level logging")
    p.add_argument(
        "--selftest", action="store_true",
        help="run the built-in rendering checks against embedded wikitext (no network, no "
             "--offline dir needed) and exit -- see selftest()",
    )
    return p.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    """Top-level orchestration. Returns the process exit code per this script's failure policy
    (module docstring): 0 whenever a corpus file was successfully written (however many pages
    were skipped below the 5% threshold), 1 on a hard failure (>5% of attempted pages failed to
    parse, an entire source could not be fetched at all, or the output could not be written) --
    and in every code-1 case, nothing is written to `args.out`.

    A whole-SOURCE fetch failure (the dump could not be downloaded/decompressed, or a wiki's API
    walk raised after exhausting its retries) is treated as a hard failure of the same severity as
    the "wrote nothing" path, not folded into the per-page skip count: PLAN-v6.md's own rationale
    ("a half-built corpus is worse than a stale one") applies at least as strongly to losing an
    entire source outright as it does to a handful of bad pages within one. This is a judgement
    call beyond what PLAN-v6.md's failure-policy paragraph states verbatim (that paragraph is
    about per-page parse failures specifically); see this task's report."""
    if mwparserfromhell is None:
        _LOG.error("the 'mwparserfromhell' package is not installed -- see requirements-ingest.txt")
        return 1

    stats = RunStats()
    fandom_raw: list = []
    weirdgloop_raw: list = []

    if args.offline:
        _LOG.info("offline mode: reading fixtures from %s (no network access)", args.offline)
        fandom_fixture, weirdgloop_fixture = read_offline_fixtures(args.offline)
        limit = args.limit
        fandom_raw = [RawPage(source="fandom", **p) for p in fandom_fixture][:limit]
        weirdgloop_raw = [RawPage(source="weirdgloop", **p) for p in weirdgloop_fixture][:limit]
    else:
        if requests is None:
            _LOG.error("the 'requests' package is not installed -- see requirements-ingest.txt")
            return 1
        user_agent = build_user_agent(args.contact)
        fandom_client = WikiClient(FANDOM_API, user_agent)
        weirdgloop_client = WikiClient(WEIRDGLOOP_API, user_agent)

        dump_path = args.dump_path or (os.path.splitext(args.out)[0] + "-fandom-dump.xml")
        try:
            if not args.dump_path or not os.path.isfile(dump_path):
                _LOG.info("downloading and decompressing the Fandom bulk dump...")
                download_fandom_dump(fandom_client, dump_path)
            _LOG.info("stream-parsing the Fandom dump (ns0 only)...")
            dump_pages = list(iter_dump_pages(dump_path, limit=args.limit))
        except Exception as exc:
            _LOG.error("could not obtain the Fandom base corpus (dump download/parse failed): %r", exc)
            return 1
        dump_timestamps = {p.title: p.timestamp for p in dump_pages}
        fandom_by_title = {p.title: p for p in dump_pages}
        _LOG.info("Fandom base: %d pages from the dump", len(dump_pages))

        try:
            delta_pages = list(
                ingest_fandom_delta(fandom_client, dump_timestamps, stats, limit=args.limit)
            )
        except Exception as exc:
            _LOG.error("could not complete the Fandom delta walk: %r", exc)
            return 1
        for p in delta_pages:
            fandom_by_title[p.title] = p
        _LOG.info("Fandom delta: %d pages newer than the dump (or new since it)", len(delta_pages))
        fandom_raw = list(fandom_by_title.values())

        try:
            weirdgloop_raw = list(
                walk_allpages(weirdgloop_client, "weirdgloop", stats, limit=args.limit)
            )
        except Exception as exc:
            _LOG.error("could not complete the Weird Gloop walk: %r", exc)
            return 1
        _LOG.info("Weird Gloop: %d pages", len(weirdgloop_raw))

    records = list(raw_pages_to_records(iter(fandom_raw + weirdgloop_raw), stats))

    floor = required_page_floor(args)
    if stats.attempted < floor:
        _LOG.error(
            "SUMMARY: hard failure -- only %d page(s) attempted (minimum plausible for this run "
            "is %d, see required_page_floor()); writing nothing. A run that fetches almost "
            "nothing is worse than a stale corpus.",
            stats.attempted, floor,
        )
        return 1

    skip_ratio = stats.skip_ratio()
    if skip_ratio > FAILURE_THRESHOLD:
        _LOG.error(
            "SUMMARY: hard failure -- %d/%d pages failed to parse (%.1f%%, over the %.0f%% "
            "threshold); writing nothing. Skipped: %s",
            stats.skipped, stats.attempted, skip_ratio * 100, FAILURE_THRESHOLD * 100,
            ", ".join(stats.skipped_titles),
        )
        return 1

    try:
        written = write_corpus(records, args.out)
    except OSError as exc:
        _LOG.error("SUMMARY: could not write %s: %r -- writing nothing", args.out, exc)
        return 1

    log_level = logging.WARNING if stats.skipped else logging.INFO
    _LOG.log(
        log_level,
        "SUMMARY: wrote %d records (%d pages) to %s -- %d/%d pages attempted, %d skipped%s",
        written, stats.succeeded, args.out, stats.succeeded, stats.attempted, stats.skipped,
        (": " + ", ".join(stats.skipped_titles)) if stats.skipped_titles else "",
    )
    return 0


def selftest() -> int:
    """`--selftest`: exercises the W6 rendering paths (data-bearing templates, wikitables)
    against small embedded wikitext fixtures -- no network, no --offline directory, no dump
    needed. Prints PASS/FAIL per check and returns 0 only if every one passed.

    This IS "add checks for the new coverage" from PLAN-v6.md's W6 section: a creature's drops
    end up attached to that creature, a wikitable row keeps its header label with its value, an
    excluded (navbox/hatnote) template disappears cleanly rather than leaking its name or params
    as noise, {{ and [[ never survive rendering, and rendering the same input twice is
    deterministic. Every fixture here is synthetic and minimal on purpose, not a real wiki page,
    so this runs instantly and offline and so a future change that breaks one of these specific
    behaviors fails here immediately -- see this task's report for how each assertion was watched
    to actually fail (by temporarily reverting the corresponding rendering change) before being
    confirmed passing again; an assertion never watched to fail is not a lock."""
    checks = []

    def check(name, condition):
        checks.append((name, bool(condition)))

    # 1. A creature's drops end up attached to that creature.
    creature_page = (
        "{{infobox creature\n| title = Test Beast\n}}\n"
        "== Drops ==\n"
        "{{drop table|{{drop row|item=Test Trophy|0star=10%}}\n"
        "{{drop row|item=Test Meat|0star=5}}}}\n"
    )
    creature_sections = dict(page_to_sections("Test Beast", creature_page))
    drops_text = creature_sections.get("Drops", "")
    check(
        "creature drops attached to the creature (item name + rate together in Drops)",
        "Test Trophy" in drops_text and "10%" in drops_text and "Test Meat" in drops_text,
    )

    # 2. A wikitable row keeps its header label with its value.
    table_page = (
        "== Damage ==\n"
        '{| class="wikitable"\n'
        "! Damage type !! Multiplier\n"
        "|-\n"
        "| Fire || 25%\n"
        "|-\n"
        "| Frost || 50%\n"
        "|}\n"
    )
    table_sections = dict(page_to_sections("Test Table Page", table_page))
    damage_text = table_sections.get("Damage", "")
    check(
        "wikitable row keeps its header label attached to its value",
        "Damage type: Fire" in damage_text and "Multiplier: 25%" in damage_text
        and "Damage type: Frost" in damage_text and "Multiplier: 50%" in damage_text,
    )

    # 3. An excluded (navbox/hatnote) template disappears cleanly; real prose is untouched.
    navbox_page = "== Notes ==\nReal prose stays. {{TestThingNav}} {{For|a disambiguation note|Other Page}}\n"
    navbox_sections = dict(page_to_sections("Test Navbox Page", navbox_page))
    notes_text = navbox_sections.get("Notes", "")
    check(
        "excluded navbox/hatnote template leaves no trace, real prose survives",
        "Real prose stays" in notes_text
        and "TestThingNav" not in notes_text
        and "disambiguation note" not in notes_text,
    )

    # 4. {{Item link}} renders inline with its quantity (or bare, when none is given).
    item_link_page = "== Recipe ==\nRequires {{Item link|Wood|10}} and {{Item link|Stone}}.\n"
    item_sections = dict(page_to_sections("Test Recipe Page", item_link_page))
    recipe_text = item_sections.get("Recipe", "")
    check(
        "Item link renders as 'Name xN' with its quantity, bare 'Name' with none",
        "Wood x10" in recipe_text and "Stone" in recipe_text,
    )

    # 5. {{ and [[ never survive rendering, across every fixture above. NOTE on this check's real
    # coverage (confirmed empirically while building this task): mwparserfromhell's
    # Wikicode.replace() RE-PARSES its string argument, so a render_* function that accidentally
    # embedded a raw but WELL-FORMED nested template/link would have it silently re-absorbed and
    # dropped by the final strip_code() call -- the same silent-content-loss failure mode W6 exists
    # to fix, not a visible brace leak, and checks 1/2/4 above (content actually present) are what
    # catch that class of regression. This check's real teeth are narrower: it catches a
    # MALFORMED/incomplete brace sequence (confirmed: strip_code() leaves an unmatched "{{" with no
    # closing "}}" as literal text rather than silently eating it), which is still a real and
    # distinct way this script could visibly leak wiki markup into the corpus.
    all_text = "\n".join(
        text
        for secs in (creature_sections, table_sections, navbox_sections, item_sections)
        for text in secs.values()
    )
    check(
        "no literal '{{' or '[[' survives in any fixture's rendered output",
        "{{" not in all_text and "[[" not in all_text,
    )

    # 6. Rendering the same input twice is deterministic (no dict/set-ordering surprise) --
    # the real-world equivalent of write_corpus()'s sort making a full run byte-stable.
    check(
        "rendering the same page twice produces identical output",
        page_to_sections("Test Beast", creature_page) == page_to_sections("Test Beast", creature_page),
    )

    ok = True
    for name, passed in checks:
        print(f"{'PASS' if passed else 'FAIL'}: {name}")
        ok = ok and passed
    print("SELFTEST:", "all checks passed" if ok else "one or more checks FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
