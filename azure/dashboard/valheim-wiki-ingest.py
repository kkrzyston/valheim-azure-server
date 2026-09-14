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


def clean_wikicode(code) -> str:
    """Render a parsed mwparserfromhell Wikicode fragment (a whole page, one section, or one
    infobox parameter's value) down to plain prose, per PLAN-v6.md step 6's sanitize list:
    HTML comments, <ref> tags, file/image links, and category links are removed outright (not
    just their brackets -- mwparserfromhell's own strip_code() leaves a File: link's caption text
    and a bare Category:Foo behind as visible prose, which is wrong for our purposes); <br> tags
    become "; " so infobox list-style values (e.g. Boar's `drops` field, several wikilinks joined
    by <br/>) stay readable instead of running together with no separator; ordinary wikilinks
    reduce to their display text and templates are dropped by strip_code()'s own default behavior
    (confirmed against real Fandom wikitext while building this script -- see this task's report).
    Finally, whitespace collapses and any literal @everyone/@here is defused, mirroring
    valheim-bot.py's own EVERYONE_RE backstop even though this text never reaches that bot's
    system prompt as anything but clearly-labelled reference data (see PLAN-v6.md "The two rules
    that make this safe").

    Never raises on malformed input: mwparserfromhell's own parse is forgiving, and the loops here
    only ever remove or replace nodes the parser already found -- there is no path that reads a
    node this function did not itself enumerate."""
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

    for link in list(code.filter_wikilinks(recursive=True)):
        title = str(link.title).strip().lower()
        if title.startswith(("file:", "image:", "category:")):
            try:
                code.remove(link)
            except ValueError:
                pass

    text = code.strip_code(normalize=True, collapse=True)
    text = _BR_RUN_RE.sub("; ", text)  # see _BR_PLACEHOLDER's comment above
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


def render_infobox_blocks(code) -> list:
    """Find every infobox in `code` (an mwparserfromhell Wikicode for a whole page) and render
    each to prose via render_infobox(), handling PLAN-v6.md step 4's two shapes:

      1. A plain top-level `{{infobox ...}}` (e.g. Boar's `{{infobox creature}}`) -- one block,
         no tab label.
      2. `{{InfoboxTabber|Label1|{{infobox ...}}|Label2|{{infobox ...}}|...}}` (e.g. Leather
         Armor's four armor pieces) -- InfoboxTabber's own params are positional label/content
         pairs (confirmed against real Fandom wikitext: params "1","2","3","4" hold "Head",
         the helmet's {{infobox armor}}, "Chest", the tunic's {{infobox armor}}, ...); each
         content slot is re-parsed for its nested infobox template(s) and rendered with its
         label from the immediately preceding positional slot.

    Only TOP-LEVEL templates are considered for case 1 (mwparserfromhell's filter_templates(...,
    recursive=False) already excludes anything nested inside another template), so an infobox
    handled via case 2 is never also emitted, unlabelled, via case 1 -- recursive=False on the
    outer scan means InfoboxTabber's own nested infoboxes never show up there at all. Returns a
    list of rendered prose blocks in document order; empty if the page has no infobox."""
    blocks = []
    for template in code.filter_templates(recursive=False):
        name = str(template.name).strip().lower()
        if name == INFOBOX_TABBER_NAME:
            params = template.params
            for i in range(0, len(params) - 1, 2):
                label = clean_wikicode(params[i].value).strip()
                nested_code = mwparserfromhell.parse(str(params[i + 1].value))
                for nested in nested_code.filter_templates(recursive=False):
                    nested_name = str(nested.name).strip().lower()
                    if nested_name.startswith(INFOBOX_NAME_PREFIX):
                        blocks.append(render_infobox(nested, tab_label=label or None))
        elif name.startswith(INFOBOX_NAME_PREFIX):
            blocks.append(render_infobox(template))
    return blocks


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
    """Parse one page's wikitext into (heading, text) pairs, per PLAN-v6.md steps 4-5. The lead
    section (heading "") carries the page's infobox rendering (if any) prepended to its prose,
    since an infobox conceptually belongs with the page's opening description, not under whatever
    heading happens to come textually first. Every other heading's section is its own (heading,
    text) pair with no infobox content (render_infobox_blocks() only looks at TOP-LEVEL templates,
    and by definition no infobox appears outside the lead in any real Valheim wiki page).

    Sections whose rendered text is empty after sanitizing (e.g. a "Gallery" heading whose only
    content was <gallery> image markup) are dropped -- an empty record is not reference material
    for anything, and W2's search() has nothing to rank it against.

    Raises whatever mwparserfromhell.parse()/get_sections() raises on genuinely malformed input;
    callers (see ingest_pages()) are responsible for turning that into a logged skip, per this
    script's failure policy -- this function itself makes no attempt to recover from a bad parse,
    so a caller can tell "this page parsed to nothing" (empty list, not a failure) apart from
    "this page could not be parsed at all" (an exception)."""
    code = mwparserfromhell.parse(wikitext)
    infobox_prose = "\n\n".join(render_infobox_blocks(code))

    sections = code.get_sections(flat=True, include_lead=True, include_headings=True)
    out = []
    seen_lead = False
    for section in sections:
        heading, body_code = heading_and_body(section)
        text = clean_wikicode(body_code)
        if heading == "" and not seen_lead:
            seen_lead = True
            text = "\n\n".join(p for p in (infobox_prose, text) if p)
        if text:
            out.append((heading, text))
    if not seen_lead and infobox_prose:
        # A page whose entire body is templates (get_sections found no lead prose at all) still
        # needs its infobox surfaced somewhere rather than silently dropped.
        out.insert(0, ("", infobox_prose))
    return out


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
