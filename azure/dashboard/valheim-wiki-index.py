#!/usr/bin/env python3
"""valheim-wiki-index.py -- SQLite FTS5 index and read-time retrieval for Hermodr's Valheim game
knowledge (PLAN-v6, task W2). Two jobs, cleanly separated:

  1. build_index(): offline, run by hand or by valheim-wiki-refresh.timer (task W4). Reads
     wiki-records.jsonl (task W1's output), dedupes matching sections across the two source
     wikis, and writes an FTS5 SQLite index atomically (wiki.db.tmp -> os.replace -> wiki.db)
     so a reader never sees a half-built index.
  2. search(): read-time, called from valheim-bot.py (task W3) off the gateway thread via
     asyncio.to_thread. Opens the on-disk index, reloads automatically when the file's mtime
     changes so a refresh takes effect without restarting the bot, and NEVER raises -- a
     missing, unreadable, or empty index is a normal "no game knowledge yet" state, not an
     error the bot should crash or apologize for.

Anti-injection note (PLAN-v6 "The two rules that make this safe"): wiki text served by search()
is reference data appended to a model's context, not instructions, and the model that reads it
has no tools -- but the QUERY string handed to search() is Discord user input, forwarded
untouched from valheim-bot.py. FTS5's query syntax has its own grammar (AND OR NOT NEAR,
"phrase quoting", column filters, trailing-* prefix search, parentheses) that has nothing to do
with SQL injection but can still throw sqlite3.OperationalError on malformed input, or -- more
subtly -- silently change the query's *meaning* (a question containing the bare word "or" would
otherwise parse as a boolean operator). _build_match_expression() defuses both: it tokenizes the
query down to bare words and re-quotes every one as an FTS5 string literal before it ever reaches
MATCH. That result is then passed to sqlite3 as a bound `?` parameter, never string-formatted
into SQL text -- belt-and-suspenders: the quoting protects FTS5's own grammar, the parameter
binding protects the surrounding SQL statement.

Stdlib only: sqlite3, json, os, re, sys, difflib, threading, argparse, time, tempfile, io,
contextlib. All standard library. Imported directly by valheim-bot.py (via importlib, like
valheim-medals.py, because of the hyphenated filename) and valheim-bot.py must stay stdlib-only
per PLAN-v6's hard rules -- so this module must too.

All paths take an env override (VALHEIM_WIKI_ROOT), matching the VALHEIM_RESTART_ROOT /
valheim-medals.py convention, so this can run against fixtures without touching /var.
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time


# ---------------------------------------------------------------- paths & config
WIKI_ROOT = os.environ.get("VALHEIM_WIKI_ROOT", "/var/lib/valheim-wiki").rstrip("/")
RECORDS_PATH_DEFAULT = os.path.join(WIKI_ROOT, "wiki-records.jsonl")
DB_PATH_DEFAULT = os.path.join(WIKI_ROOT, "wiki.db")

# BM25 column weights, in the same order as the FTS5 table's indexed columns below: title far
# above heading above text. Wiki titles ARE the entities (PLAN-v6 W2.3) -- a title hit
# ("Fenring") is almost always the right page even before looking at body text. Lower bm25()
# values rank better in SQLite; ORDER BY rank ASC puts the best match first.
_BM25_WEIGHTS = (10.0, 5.0, 1.0)  # title, heading, text

# ---------------------------------------------------------------- curated-source ranking boost
# A hand-curated fact (source == "curated", from valheim-curated-numbers.jsonl, merged in by the
# ingest) was cross-verified word-for-word identical on BOTH wikis before being included -- it is
# the highest-confidence record in the index, and PLAN-v6 wants it to win a tie or near-tie
# against the wiki text it was derived from. But "curated wins" must mean "wins among comparable
# matches", never "wins regardless of relevance": a curated record that only weakly, incidentally
# matches a query must not leapfrog a wiki record that is strongly on-topic for that same query.
#
# Mechanism: _rank_with_curated_boost() below re-sorts an already-bm25-ranked candidate pool.
# rows arrive sorted ascending by raw bm25 (more negative == better match; see _BM25_WEIGHTS'
# comment), so the first row's rank is already the best (most negative) raw score in the pool --
# call it best_rank. A curated row is only boosted if its OWN raw rank is already within
# _CURATED_COMPARABLE_RATIO of best_rank (i.e. it is genuinely "in contention" for the top spot,
# not just present); a curated row that fails that comparability test is left at its natural,
# unboosted position.
#
# Two mechanisms considered and rejected:
#   - A FIXED rank offset (`rank - N`) moves a weak match (rank near 0) by exactly the same
#     absolute amount as a strong one (rank far below 0). Any N large enough to matter for a
#     genuinely weak curated hit is also large enough to vault it over a strongly-matching,
#     unrelated wiki hit -- there is no single N that is "enough" for one case and "not too much"
#     for the other, because the two cases need opposite answers to the same arithmetic.
#   - A flat MULTIPLIER on every curated row's raw bm25 (`rank * B`) was measured against the
#     real index (see the curated-boost selftest section) and found unsafe on its own: BM25 term-
#     frequency saturation compresses the gap between a "barely matches" row and a "matches very
#     strongly" row into less than a 2x difference in observed magnitude, so a multiplier large
#     enough to reliably promote a comparably-strong curated row (~1.3x+) can also be large enough
#     to invert a weak-vs-strong pair. A multiplier is only safe when it is gated behind a
#     comparability check first -- which is exactly what the ratio test below does, using the
#     multiplier only to break the tie once comparability is already established.
# Combining a comparability GATE with a multiplier applied only after the gate passes gets both
# properties at once: comparable matches reliably re-order (boost factor chosen, with margin,
# above 1 / _CURATED_COMPARABLE_RATIO so a row right at the threshold still clears the current
# best), and a non-comparable (weak) curated match is never touched, so it cannot invert a
# genuinely stronger, unrelated wiki match. See cmd_selftest() for both directions locked with
# real bm25 scores from a real (temporary) FTS5 index, not hand-typed numbers.
_CURATED_SOURCE = "curated"
_CURATED_COMPARABLE_RATIO = 0.8  # curated must already be >= 80% as strong as the current best
_CURATED_BOOST_FACTOR = 1.5      # > 1 / 0.8 = 1.25, so a row right at the threshold still wins

# _search_impl() fetches this many candidates (by raw bm25) before boosting and truncating to the
# caller's k, so a comparably-strong curated row sitting just outside a naive top-k window still
# gets a chance to be found and promoted, without scanning the whole corpus for a broad query.
_CANDIDATE_POOL_MULTIPLIER = 5
_CANDIDATE_POOL_MIN = 20

# Anti-DoS caps on untrusted query text -- a Discord message body, of arbitrary length and
# content, reaches _build_match_expression() on every question.
_MAX_QUERY_CHARS = 2000
_MAX_TERMS = 24
_MAX_TERM_CHARS = 64

# Dedupe threshold for step 5 -- see _sections_materially_differ()'s docstring for the full
# rule and reasoning.
_SAME_TEXT_RATIO_THRESHOLD = 0.75

# Small, hand-maintained map from things players actually type to the wiki vocabulary FTS5
# needs to see. Extend freely -- it's just a dict. Keys are matched as a lowercase substring of
# the raw question; values are extra search terms OR'd into the query (see
# _build_match_expression). This is the "known risk, accepted" mitigation from PLAN-v6: it does
# not fix Old Norse phrasing defeating English-title retrieval, it just closes gaps we already
# know about -- synonym stat names, boss nicknames, and a couple of common misspellings.
ALIASES = {
    "frost resistance": "Resistance",
    "fire resistance": "Resistance",
    "poison resistance": "Resistance",
    "lightning resistance": "Resistance",
    "spirit resistance": "Resistance",
    "blunt resistance": "Resistance",
    "slash resistance": "Resistance",
    "pierce resistance": "Resistance",
    "stagger": "Resistance",
    "knockback": "Resistance",
    "the deer": "Eikthyr",
    "the ent": "The Elder",
    "swamp thing": "Bonemass",
    "the dragon": "Moder",
    "the queen": "Seeker Queen",
    "fenrir": "Fenring",
    "grey dwarf": "Greydwarf",
    "greydwarves": "Greydwarf",
}


# ---------------------------------------------------------------- query sanitization
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

# Low-signal English interrogatives/auxiliaries/articles/prepositions. A natural-language
# question like "how much health does a Boar have" tokenizes to 7 OR'd terms, but only "health"
# and "boar" say anything about what the user wants -- the other five are grammatical scaffolding
# that also happens to appear constantly in ordinary wiki prose. Because _build_match_expression
# ORs every term and bm25() sums a document's per-term contributions, a long, wordy, UNRELATED
# page (a generic "Health" article, a "Poison > Stacking" section) that racks up many incidental
# matches on "does"/"have"/"a"/"the" can out-score a short, precisely-relevant page that matches
# only the one or two real entity terms. Dropping this scaffolding before the query ever reaches
# FTS5 lets the entity terms carry the full weight of the match, instead of being diluted 2-of-7.
#
# Verified against the actual corpus before being committed to, per PLAN-v6's rule that a
# stopword-looking word must not be assumed safe to drop just because it looks like filler: every
# word below was checked with a direct MediaWiki `action=query&titles=...` exact-title lookup
# against BOTH source wikis (valheim.fandom.com and valheim.weirdgloop.org, checked 2026-09-14)
# and confirmed to NOT be a real page title on either -- none of them is a Valheim entity this
# bot needs to be able to find by name. See cmd_selftest() for that same check kept as a live
# regression: every curated title and ALIASES canonical value is re-tokenized and checked against
# this set on every test run, so a future edit can't silently add a real entity's name here
# without a test failing.
#
# Deliberately English-only and exact-token-membership-based (never substring/regex removal on
# the raw query text) so Old Norse and any other non-English phrasing is untouched by
# construction -- a token like "heilsa" or "hvat" simply never appears in this set, and there is
# no code path here that could partially mangle a non-Latin or accented token.
_STOPWORDS = frozenset({
    "how", "much", "many", "what", "which", "where", "when",
    "does", "do", "did", "is", "are", "was", "were", "have", "has", "had",
    "the", "a", "an", "of", "for", "in", "on", "to", "it",
})


def _strip_stopwords(terms):
    """Drop _STOPWORDS members from `terms` so the remaining entity words carry the query's full
    weight -- but NEVER return an empty list. If every term is a stopword (e.g. a query that is
    itself just filler, like "what is it"), stripping would throw away the entire query and leave
    nothing for FTS5 to match; the caller is better served by falling back to the original,
    unfiltered terms (a broad, noisy match) than by _build_match_expression treating the query as
    empty and returning no results at all. Order is preserved; duplicates are left exactly as
    _build_match_expression already handled them before this function existed."""
    filtered = [t for t in terms if t not in _STOPWORDS]
    return filtered if filtered else terms


def _build_match_expression(query):
    """Turn arbitrary, untrusted query text into a safe FTS5 MATCH expression, or None if
    there is nothing searchable in it. Never raises -- any input that isn't a usable string
    (None, bytes, an int, ...) is treated as empty rather than erroring.

    See the module docstring's anti-injection note for the two-layer defense this implements.
    """
    if not isinstance(query, str):
        return None

    query = query[:_MAX_QUERY_CHARS]
    lowered = query.lower()

    terms = [t.lower() for t in _TOKEN_RE.findall(query)]

    # Alias expansion against the raw lowered query (not the token list), so multi-word keys
    # like "frost resistance" match as a phrase.
    for key, canonical in ALIASES.items():
        if key in lowered:
            terms.extend(t.lower() for t in _TOKEN_RE.findall(canonical))

    if not terms:
        return None

    # Strip English filler AFTER alias expansion (so an alias's canonical terms are also subject
    # to it -- moot today since no ALIASES value tokenizes to a stopword, but harmless either
    # way) and BEFORE truncating to _MAX_TERMS, so the budget of terms that actually reach FTS5
    # is spent on entity words first, not used up on "does"/"have"/"a". _strip_stopwords()
    # guarantees this can never turn a non-empty `terms` into an empty one.
    terms = _strip_stopwords(terms)

    quoted = []
    for t in terms[:_MAX_TERMS]:
        t = t[:_MAX_TERM_CHARS].replace('"', '""')  # FTS5 string-literal escaping; \w+ never
        if t:                                        # actually produces a quote, but be sure
            quoted.append('"%s"' % t)

    if not quoted:
        return None
    return " OR ".join(quoted)


# ---------------------------------------------------------------- read-time cache (step 6)
_cache_lock = threading.Lock()
_cache = {"path": None, "mtime": None, "conn": None}

# ---------------------------------------------------------------- H1: observable failures
# search() must never raise (see its own docstring) -- but "never raise" and "never log" are
# different promises, and the original code kept only the first one. A sqlite3.OperationalError
# from a stale/foreign wiki.db (wrong or missing schema), a disk error, or any other genuine
# failure was degrading to a bare [], byte-for-byte identical to an honest "nothing matched".
# Nobody could ever tell the index was broken from the log, because there was nothing IN the log.
#
# The fix keeps the no-raise contract (still catch-and-return-[]) but adds one thing: an ERROR
# line to stderr before returning, so a broken index is finally distinguishable from an empty
# result. It is rate-limited per DISTINCT error signature (exception type + message) rather than
# per call, so a persistent failure -- the same broken wiki.db answering every Discord question --
# logs loudly once and then stays quiet for _ERROR_LOG_COOLDOWN_SECONDS instead of flooding the
# log once per question. A genuinely NEW/different failure (a different exception type or
# message) is never suppressed by an earlier, unrelated one already being on cooldown.
_ERROR_LOG_COOLDOWN_SECONDS = 300  # re-announce an unchanged failure at most once per 5 minutes
_logged_errors_lock = threading.Lock()
_logged_errors = {}  # error signature ("Type: message") -> time.monotonic() last logged


def _log_search_failure(exc):
    """Emit a rate-limited ERROR line for a search()-path failure. See the module-level comment
    above for the full rationale. Never raises itself -- a logging call must not be the thing
    that turns a handled failure into an unhandled one."""
    signature = f"{type(exc).__name__}: {exc}"
    now = time.monotonic()
    try:
        with _logged_errors_lock:
            last = _logged_errors.get(signature)
            if last is not None and now - last < _ERROR_LOG_COOLDOWN_SECONDS:
                return
            _logged_errors[signature] = now
        print(
            "valheim-wiki-index: search() failed, degrading to [] per its no-raise contract "
            "(this is a bug in the index or its storage, NOT a legitimate no-match -- see H1): "
            + signature,
            file=sys.stderr,
        )
    except Exception:
        pass


def _reset_error_log_for_tests():
    """Test-only helper: clear the rate-limit state so a --selftest run doesn't have its second
    assertion suppressed by the first. Not part of the module's public contract."""
    with _logged_errors_lock:
        _logged_errors.clear()


def _get_connection(db_path):
    """Return a live sqlite3 connection to db_path, reopening it whenever the file's mtime has
    changed since the last call (step 6: a refresh takes effect without restarting the bot).
    Returns None -- never raises -- if the file is missing or cannot be opened.

    "Missing" (FileNotFoundError) is the normal, silent, expected state before the first index
    build has ever run, or between a rebuild and the next -- NOT logged. Anything else that keeps
    the file from being opened (permissions, a disk error, a corrupt/foreign file that connects
    but fails PRAGMA) is a genuine failure per H1 and IS logged, via _log_search_failure()."""
    try:
        mtime = os.stat(db_path).st_mtime
    except FileNotFoundError:
        return None
    except OSError as exc:
        _log_search_failure(exc)
        return None

    with _cache_lock:
        if (
            _cache["conn"] is not None
            and _cache["path"] == db_path
            and _cache["mtime"] == mtime
        ):
            return _cache["conn"]

        old_conn = _cache["conn"]
        try:
            new_conn = sqlite3.connect(db_path, check_same_thread=False)
            new_conn.execute("PRAGMA query_only = ON")
        except sqlite3.Error as exc:
            _log_search_failure(exc)
            return None

        _cache["conn"] = new_conn
        _cache["path"] = db_path
        _cache["mtime"] = mtime

    if old_conn is not None and old_conn is not new_conn:
        try:
            old_conn.close()
        except sqlite3.Error:
            pass
    return new_conn


def _reset_cache_for_tests():
    """Test-only helper: drop the cached connection so the next search() call starts clean.
    Not part of the module's public contract."""
    with _cache_lock:
        conn = _cache["conn"]
        _cache["conn"] = None
        _cache["path"] = None
        _cache["mtime"] = None
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass


# ---------------------------------------------------------------- search() -- the fixed interface
def search(query, k=3, max_chars=4000):
    """Return up to k section records, highest-ranked first, whose combined `text` fields
    total <= max_chars. Each record:
        {"title": str, "heading": str, "text": str,
         "source": "weirdgloop" | "fandom" | "curated", "revid": int, "url": str}
    Ranking is bm25() with a source-aware boost for "curated" records that are comparably
    strong matches -- see the module-level comment above _CURATED_SOURCE for the mechanism and
    why a comparability gate, not a blind boost, is what keeps a weak curated match from
    outranking a strong, unrelated wiki match.
    Returns [] when the index is missing, unreadable, or nothing matches. Never raises.

    "Missing" and "nothing matches" are both legitimate, silent [] outcomes -- but "unreadable"
    (a stale/foreign wiki.db with the wrong schema, a corrupt file, a disk error) is a genuine
    bug and, per H1, must not look identical to those in the log. See _log_search_failure()'s
    module-level comment for the full rationale; this is the other half of it (the first half is
    _get_connection() failing to even open the file -- this one is everything that can go wrong
    once it IS open, most commonly `sqlite3.OperationalError: no such table` against an old or
    wrong-shaped wiki.db)."""
    try:
        return _search_impl(query, k, max_chars)
    except Exception as exc:
        # Any failure here still degrades to "no game knowledge for this question" -- per
        # PLAN-v6, [] is a normal outcome, not an error, and W3 falls back to the model's own
        # (flagged unverified) knowledge. A broken index must never take the whole bot down with
        # it -- but it must show up in the log, which is what H1's fix actually was.
        _log_search_failure(exc)
        return []


def _rank_with_curated_boost(rows):
    """Re-order a bm25-ranked candidate pool so a comparably-strong "curated" row moves ahead of
    the row(s) it is comparable to, without ever promoting a weakly-matching curated row past a
    strongly-matching one. See the module-level comment above _CURATED_SOURCE for the mechanism
    and why it takes this shape; see cmd_selftest() for both directions locked with real bm25
    scores.

    `rows` must already be sorted ascending by raw bm25 (each row's LAST element -- SQL's
    `ORDER BY rank` already guarantees this), so rows[0]'s rank is the best (most negative) raw
    score anywhere in the pool -- no extra query needed to find it. Returns a new list; never
    raises and never changes which rows are present, only their order (sorted() is stable, so a
    row that doesn't qualify for a boost keeps its original raw-bm25 relative position)."""
    if not rows:
        return rows
    best_rank = rows[0][-1]

    def sort_key(row):
        rank = row[-1]
        source = row[3]
        if (
            source == _CURATED_SOURCE
            and best_rank < 0
            and rank <= best_rank * _CURATED_COMPARABLE_RATIO
        ):
            return rank * _CURATED_BOOST_FACTOR
        return rank

    return sorted(rows, key=sort_key)


def _search_impl(query, k, max_chars):
    if not isinstance(k, int) or isinstance(k, bool) or k <= 0:
        k = 3
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars <= 0:
        max_chars = 4000

    match_expr = _build_match_expression(query)
    if match_expr is None:
        return []

    conn = _get_connection(DB_PATH_DEFAULT)
    if conn is None:
        return []

    weight_title, weight_heading, weight_text = _BM25_WEIGHTS
    # Fetch a modestly larger candidate pool than k (see _CANDIDATE_POOL_MULTIPLIER/_MIN's
    # comment) so the curated boost below has room to promote a comparably-strong curated row
    # that raw bm25 alone would have placed just outside the caller's requested k.
    candidate_limit = max(k * _CANDIDATE_POOL_MULTIPLIER, _CANDIDATE_POOL_MIN)
    sql = (
        "SELECT title, heading, text, source, revid, url, "
        "bm25(sections, ?, ?, ?) AS rank "
        "FROM sections WHERE sections MATCH ? "
        "ORDER BY rank LIMIT ?"
    )
    with _cache_lock:  # serialize access to the shared connection across caller threads
        rows = conn.execute(
            sql, (weight_title, weight_heading, weight_text, match_expr, candidate_limit)
        ).fetchall()

    rows = _rank_with_curated_boost(rows)[:k]

    results = []
    total = 0
    for title, heading, text, source, revid, url, _rank in rows:
        text = text or ""
        if total == 0 and len(text) > max_chars:
            # The single best match alone exceeds the budget -- truncate rather than return
            # nothing for a legitimately relevant hit. Combined total still <= max_chars.
            text = text[:max_chars]
        elif total + len(text) > max_chars:
            break
        results.append(
            {
                "title": title,
                "heading": heading,
                "text": text,
                "source": source,
                "revid": revid,
                "url": url,
            }
        )
        total += len(text)
        if len(results) >= k:
            break
    return results


# ---------------------------------------------------------------- build_index() (steps 1-2, 5)
def _verify_fts5(conn):
    """Step 1: confirm FTS5 actually exists before building on it. Per PLAN-v6, if it is
    absent we stop and report rather than silently hand-rolling a LIKE-based fallback."""
    try:
        conn.execute("CREATE VIRTUAL TABLE _fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE _fts5_probe")
        return True
    except sqlite3.OperationalError:
        return False


_WHITESPACE_RE = re.compile(r"\s+")
_NUMBER_RE = re.compile(r"\d+")


def _normalize_for_compare(text):
    return _WHITESPACE_RE.sub(" ", (text or "").strip().lower())


# C3 fix: a small, known vocabulary of relational words that game-wiki sections use to name a
# short, specific answer -- a damage type, a status, an item list. Maps every inflection this
# code recognizes down to one canonical bucket, so "weak"/"weakness"/"weaknesses" all compare
# against each other regardless of which form either source used.
#
# See _sections_materially_differ()'s docstring for why this targeted, pattern-based check --
# not a general "do the two texts use different words" comparison -- is the right level of
# matching for telling "reworded" apart from "contradicted".
_RELATION_KEYWORDS = {
    "weak": "weakness", "weakness": "weakness", "weaknesses": "weakness",
    "resist": "resistance", "resistant": "resistance", "resistance": "resistance",
    "immune": "immunity", "immunity": "immunity",
    "vulnerable": "vulnerability", "vulnerability": "vulnerability",
    "susceptible": "vulnerability",
    "drop": "drops", "drops": "drops",
    "biome": "biome", "biomes": "biome",
    "found": "location", "located": "location",
}

# Matches a keyword from the table above, an optional "to" and/or ":" separator (covering both
# "weak to fire" and "weakness: fire" phrasings), then captures up to 80 raw characters of
# whatever follows -- an outer safety cap only; _extract_relations() below does the real work of
# deciding how much of that raw text is actually part of the answer (the char class deliberately
# excludes ".", so "...fire.\nresistant" stops the raw capture at "fire" rather than swallowing
# the next sentence, but a comma or a bare "and" does NOT stop it here -- see below for why that
# still has to be handled after the fact, not by tightening this regex further).
_RELATION_VALUE_RE = re.compile(
    r"\b(" + "|".join(
        sorted((re.escape(k) for k in _RELATION_KEYWORDS), key=len, reverse=True)
    ) + r")\b"
    r"\s*(?:to)?\s*:?\s*"
    r"([a-z][a-z0-9 ,/&'-]{0,79})",
    re.IGNORECASE,
)

# A relation VALUE is a short entity or a short list of them ("fire", "frost, poison") -- never
# an arbitrary run of prose. These words end the value the instant they appear, because they
# signal a NEW clause starting, not more of the answer: FANBOYS conjunctions, relative pronouns,
# and common linking verbs (which show up constantly in the clause an editor tacks on after the
# fact, e.g. ", and IS tameable"). This is what actually fixed the false positive below -- the
# regex's raw capture above still swallows straight through a bare comma or "and" (that is what
# lets a real comma-separated list like "frost, poison" through), so without this second pass
# every trailing clause an independent edit adds after a recognized keyword would silently
# become part of the "value" and manufacture a disagreement out of two sections that assert the
# exact same fact.
_CLAUSE_BOUNDARY_RE = re.compile(
    r"\b(?:and|but|nor|or|so|yet|because|which|that|while|although|though|"
    r"is|are|was|were|has|have|had)\b|;",
    re.IGNORECASE,
)

# The other half of "short entity or short list, not prose": even after clause-boundary
# truncation, a candidate item must still look like a name, not a fragment. Longer than this many
# words/characters, or a single leftover function word, and it is discarded rather than guessed
# at -- per _sections_materially_differ()'s "fail toward keeping both" rule, an item this function
# declines to recognize simply does not participate in the comparison; it never gets asserted as
# either "the same" or "different".
_RELATION_MAX_WORDS_PER_ITEM = 3
_RELATION_MAX_ITEM_CHARS = 30
_RELATION_ITEM_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "and", "or", "but",
    "to", "of", "in", "on", "at", "for", "with", "it", "its", "this", "that",
})


def _extract_relations(normalized_text):
    """Pull {relation: set(values)} pairs -- e.g. {"weakness": {"fire"}, "resistance": {"frost",
    "poison"}} -- out of wiki prose, however it phrases them: a label ("weakness: fire") or
    inline ("weak to fire"), a single value or a comma/slash/&-separated list.

    Demonstrated false positive this function used to produce (found by a reviewer, before the
    clause-boundary and per-item bounds below existed): "...found in the Meadows biome." vs
    "...found in the Meadows biome, and is tameable." -- a second wiki editor merely adding a
    clause after the same fact. The old unbounded capture read straight through the comma and
    "and" and returned {"in the meadows biome"} vs {"in the meadows biome", "is tameable"} --
    two DIFFERENT sets, reported as a material disagreement, even though neither text disagrees
    with the other about anything. Weird Gloop is an independently-edited fork of Fandom, so "one
    copy has an extra clause the other doesn't" is the NORMAL difference between them, not the
    exceptional one -- a rule that fires on this fires on a large fraction of real pages, and
    enough false alarms teaches a reader to ignore the true ones, which is the failure this
    function exists to prevent, not the one it was built to catch (see C3's docstring above).

    Best-effort and deliberately loose in the OTHER direction still: this is only ever used to
    find REASONS to keep both copies in _sections_materially_differ(), never to decide anything
    is safe to collapse on its own, so an over-eager match here still costs at most one harmless
    extra duplicate, never a hidden contradiction -- the bounds below trade away some of that
    over-eagerness specifically because it was producing FALSE conflicts, not because false
    conflicts and hidden contradictions are equally bad (they are not: see _sections_materially_
    differ()'s docstring for why this whole rule fails toward "different" on any doubt)."""
    relations = {}
    for m in _RELATION_VALUE_RE.finditer(normalized_text):
        canonical = _RELATION_KEYWORDS[m.group(1).lower()]
        raw_value = m.group(2)
        boundary = _CLAUSE_BOUNDARY_RE.search(raw_value)
        if boundary is not None:
            raw_value = raw_value[: boundary.start()]
        raw_value = raw_value.strip(" ,.")
        if not raw_value:
            continue

        items = set()
        for candidate in re.split(r"\s*,\s*|\s*/\s*|\s*&\s*", raw_value):
            candidate = candidate.strip(" .")
            if not candidate:
                continue
            words = candidate.split()
            if not words or len(words) > _RELATION_MAX_WORDS_PER_ITEM:
                continue  # too long to be a short entity/list item -- not a relation at all
            if len(candidate) > _RELATION_MAX_ITEM_CHARS:
                continue
            if len(words) == 1 and words[0] in _RELATION_ITEM_STOPWORDS:
                continue  # a stray leftover function word, not a named answer
            items.add(candidate)
        if items:
            relations.setdefault(canonical, set()).update(items)
    return relations


def _sections_materially_differ(text_a, text_b):
    """Step 5's dedupe rule -- decides whether two same-(title, heading) sections from Fandom and
    Weird Gloop are safe to collapse to one copy, or must both be kept because they disagree on a
    fact. Two normalized (lowercased, whitespace-collapsed) texts are the SAME only if ALL of:
      (a) byte-identical after normalization (short-circuits everything below), OR
      (b) their multiset of numeric tokens matches EXACTLY, AND
      (c) neither text names a different value for the same short-answer attribute this code
          recognizes -- weakness, resistance, immunity, vulnerability, drops, biome, location --
          whether phrased as a label ("Weakness: fire") or inline ("weak to fire") -- see
          _extract_relations(), AND
      (d) a difflib.SequenceMatcher ratio on the normalized text is >= _SAME_TEXT_RATIO_THRESHOLD.
    Any ONE of a numeric mismatch, a recognized-attribute mismatch, or a low ratio is sufficient
    on its own to call the pair "different" and keep both.

    Why three checks and not the ratio alone (this function's original, too-narrow form): a pure
    edit-distance ratio measures how much TEXT changed, not whether the ANSWER changed, and those
    are not the same thing. Demonstrated case that motivated this rewrite:
        fandom     = "...weakness: fire\\nresistant to: frost, poison..."
        weirdgloop = "...weakness: frost\\nresistant to: fire, poison..."
    These two score a difflib ratio of 0.958 (they share almost every character) and have
    IDENTICAL numeric tokens (none) -- the old numeric-only rule called them "the same" and
    silently kept one copy, discarding a directly contradictory fact. Swapping "fire" for "frost"
    is a small, cheap edit by character count, which is exactly the property that makes ratio the
    wrong instrument here: PLAN-v6's design goal is "a confidently-stated wrong number is the
    exact failure to avoid" -- and a confidently-stated wrong weakness, biome, or drop is exactly
    as bad, for the same reason, even though no digit is involved.

    (c) is deliberately narrow and pattern-based rather than a general "do the two texts use
    different words" check: comparing whole content-word sets would flag nearly every reworded
    sentence as "different" (synonyms, added clauses, reordered lists) and defeat dedup entirely.
    Instead it targets the specific shape a wiki fact-swap actually takes -- a short, structured
    answer sitting right after one of a small, known vocabulary of relational words -- which is
    precisely what catches the case above without needing to parse prose in general.

    This is not a claim that (c)'s keyword list is exhaustive, or that (d)'s ratio catches every
    remaining non-numeric, non-keyword disagreement (an entity-name swap in a sentence with no
    recognized keyword, for instance, is still only caught if it drags the ratio below threshold).
    Where this function is uncertain, it is designed to fail toward "materially different": a
    harmless duplicate section shown to the user costs a little redundancy; a fact silently
    discarded because it happened to be phrased in a way this function doesn't recognize is the
    exact failure PLAN-v6 exists to avoid. That is why (b), (c), and (d) are ANDed for "same" --
    every one of them has to agree the pair is safe to collapse -- but any single one of them
    disagreeing is enough to keep both.
    """
    norm_a = _normalize_for_compare(text_a)
    norm_b = _normalize_for_compare(text_b)
    if norm_a == norm_b:
        return False
    if sorted(_NUMBER_RE.findall(norm_a)) != sorted(_NUMBER_RE.findall(norm_b)):
        return True
    rel_a = _extract_relations(norm_a)
    rel_b = _extract_relations(norm_b)
    for key in rel_a.keys() & rel_b.keys():
        if rel_a[key] != rel_b[key]:
            return True
    ratio = difflib.SequenceMatcher(None, norm_a, norm_b).ratio()
    return ratio < _SAME_TEXT_RATIO_THRESHOLD


def _load_records(records_path):
    """Read wiki-records.jsonl (W1's output). A line that isn't valid JSON, or is missing a
    required field, is logged and skipped -- never lets one bad line abort the whole build."""
    records = []
    skipped = 0
    with open(records_path, "r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                print(
                    "valheim-wiki-index: skipping unparseable line %d: %s" % (line_no, exc),
                    file=sys.stderr,
                )
                skipped += 1
                continue
            if not isinstance(rec, dict) or not all(
                key in rec for key in ("title", "heading", "text", "source", "revid", "url")
            ):
                print(
                    "valheim-wiki-index: skipping line %d: missing required field(s)" % line_no,
                    file=sys.stderr,
                )
                skipped += 1
                continue
            records.append(rec)
    return records, skipped


def _dedupe(records):
    """Group by (title, heading) and apply step 5's rule. Preserves first-seen order of
    groups; within a group, any record whose source is neither 'fandom' nor 'weirdgloop', or
    that duplicates a source already seen in the group, is passed through untouched -- this
    function never silently drops a record it doesn't have an explicit rule for."""
    groups = {}
    order = []
    for rec in records:
        key = (rec.get("title"), rec.get("heading"))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(rec)

    kept = []
    for key in order:
        by_source = {}
        others = []
        for rec in groups[key]:
            src = rec.get("source")
            if src in ("fandom", "weirdgloop") and src not in by_source:
                by_source[src] = rec
            else:
                others.append(rec)

        fandom = by_source.get("fandom")
        weirdgloop = by_source.get("weirdgloop")
        if fandom and weirdgloop:
            if _sections_materially_differ(fandom.get("text", ""), weirdgloop.get("text", "")):
                kept.append(fandom)
                kept.append(weirdgloop)
            else:
                kept.append(fandom)  # identical enough -- keep the commercial-clean licence
        elif fandom:
            kept.append(fandom)
        elif weirdgloop:
            kept.append(weirdgloop)
        kept.extend(others)
    return kept


def build_index(records_path=None, db_path=None):
    """Build (or rebuild) the FTS5 index from wiki-records.jsonl. Offline / operator path --
    unlike search(), this DOES raise on a real failure (FTS5 missing, input unreadable) so a
    timer run fails loudly and W4's unit leaves the previous wiki.db in place, rather than this
    function silently producing a broken or empty index. Returns a stats dict on success."""
    records_path = records_path or RECORDS_PATH_DEFAULT
    db_path = db_path or DB_PATH_DEFAULT

    probe = sqlite3.connect(":memory:")
    try:
        fts5_ok = _verify_fts5(probe)
    finally:
        probe.close()
    if not fts5_ok:
        raise RuntimeError(
            "SQLite FTS5 is not available in this Python's sqlite3 build (PLAN-v6 W2.1: stop "
            "and report rather than silently falling back -- the owner decides)."
        )

    records, skipped = _load_records(records_path)
    kept = _dedupe(records)

    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    tmp_path = db_path + ".tmp"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)

    conn = sqlite3.connect(tmp_path)
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE sections USING fts5("
            "title, heading, text, "
            "source UNINDEXED, revid UNINDEXED, url UNINDEXED, timestamp UNINDEXED)"
        )
        conn.executemany(
            "INSERT INTO sections (title, heading, text, source, revid, url, timestamp) "
            "VALUES (:title, :heading, :text, :source, :revid, :url, :timestamp)",
            (
                {
                    "title": r.get("title", ""),
                    "heading": r.get("heading", ""),
                    "text": r.get("text", ""),
                    "source": r.get("source", ""),
                    "revid": r.get("revid", 0),
                    "url": r.get("url", ""),
                    "timestamp": r.get("timestamp", ""),
                }
                for r in kept
            ),
        )
        conn.commit()
    finally:
        conn.close()

    os.replace(tmp_path, db_path)  # atomic swap -- a reader never sees a half-built index

    return {
        "records_in": len(records),
        "skipped_unparseable": skipped,
        "sections_kept": len(kept),
    }


# ---------------------------------------------------------------- selftest (stdlib-only, no network)
def cmd_selftest():
    """Stdlib-only, no-network selftest for this module's two responsibilities: the dedupe rule
    (_sections_materially_differ()/_dedupe(), used by build_index()) and the read-time search()
    contract, including H1's fix (a broken index must be observable in the log, not silently
    identical to an honest empty result). Real assertions, not prints -- a failed one exits this
    process non-zero.

    Before this function existed, this module -- an entire stdlib-only, no-network module built
    specifically for fast direct testing (see the module docstring) -- had zero direct tests of
    its own; every one of its behaviours was only ever exercised indirectly, through
    valheim-bot.py's own --selftest importing it. That was the single highest-value test gap a
    reviewer found in PLAN-v6 W2/W6."""
    global DB_PATH_DEFAULT
    all_ok = True

    print("--- _sections_materially_differ(): the C3 dedupe-rule fix ---")
    # Two separate failure modes, two separate locks -- a rule that only ever answers "differ"
    # would pass a suite that only tests the FALSE-NEGATIVE direction (a real conflict hidden),
    # so the FALSE-POSITIVE direction (a non-conflict reported) gets its own cases below, not
    # just a comment saying it was checked by hand.
    cases = [
        # -- false-negative direction: a real disagreement must still be caught --
        ("non-numeric disagreement (required minimum case)",
         "weak to fire", "weak to frost", True),
        ("identical text", "Fenring is weak to fire.", "Fenring is weak to fire.", False),
        ("numeric disagreement (the pre-existing rule, still must hold)",
         "Health: 10", "Health: 12", True),
        ("reworded, same numbers, no recognized keyword -- still collapses to one copy",
         "This creature has 10 health and deals 4 damage each hit.",
         "This creature has 10 health and deals 4 damage per hit.", False),
        ("label-style, comma-list value swap (the exact case a reviewer demonstrated the "
         "old numeric-only rule missing)",
         "...weakness: fire\nresistant to: frost, poison...",
         "...weakness: frost\nresistant to: fire, poison...", True),
        # -- false-positive direction: an added clause is NOT a disagreement (a second reviewer
        # demonstrated _extract_relations() reporting one anyway, before clause-boundary and
        # per-item bounds were added -- see _extract_relations()'s docstring for the full case) --
        ("an added trailing clause after a recognized keyword is not a value swap",
         "The Boar is a creature found in the Meadows biome.",
         "The Boar is a creature found in the Meadows biome, and is tameable.", False),
        ("an incidental, keyword-free wording change (e.g. a renamed CSS class in the "
         "underlying markup) must never be reported as a fact disagreement",
         "Rendered with a tooltip using the icon-boar-alpha style.",
         "Rendered with a tooltip using the icon-boar-beta style.", False),
    ]
    for desc, text_a, text_b, expected in cases:
        got = _sections_materially_differ(text_a, text_b)
        ok = got is expected
        print(f"{'PASS' if ok else 'FAIL'} {desc}: got {got} (want {expected})")
        all_ok = all_ok and ok

    print("\n--- _dedupe(): a non-numeric disagreement keeps BOTH copies, never picks a winner ---")
    conflicting_records = [
        {"title": "Fenring", "heading": "Weaknesses",
         "text": "...weakness: fire\nresistant to: frost, poison...",
         "source": "fandom", "revid": 1, "url": "https://example.invalid/fandom/fenring"},
        {"title": "Fenring", "heading": "Weaknesses",
         "text": "...weakness: frost\nresistant to: fire, poison...",
         "source": "weirdgloop", "revid": 2, "url": "https://example.invalid/weirdgloop/fenring"},
    ]
    kept = _dedupe(conflicting_records)
    kept_sources = sorted(r["source"] for r in kept)
    dedupe_ok = kept_sources == ["fandom", "weirdgloop"]
    print(f"{'PASS' if dedupe_ok else 'FAIL'} both sources survive dedupe for a non-numeric "
          f"disagreement -- this is C3: it used to silently keep only ['fandom']: "
          f"{kept_sources!r}")
    all_ok = all_ok and dedupe_ok

    print("\n--- _dedupe(): curated records pass through untouched, never collapsed ---")
    # A previous agent reported "_dedupe() already passes non-fandom/weirdgloop sources through
    # untouched" -- verified directly here rather than taken on trust, then pinned with an
    # assertion so a future change to the by_source/others split can't silently start treating a
    # third source as fandom/weirdgloop-shaped.
    curated_passthrough_records = [
        # Two curated records sharing a (title, heading) with EACH OTHER -- dedupe must not
        # collapse them just because they share a key, the way it collapses a matching
        # fandom/weirdgloop pair.
        {"title": "Bonemass", "heading": "Curated: Boss Stats",
         "text": "Bonemass: weak to Blunt and Frost.",
         "source": "curated", "revid": 501, "url": "https://example.invalid/curated/bonemass-a"},
        {"title": "Bonemass", "heading": "Curated: Boss Stats",
         "text": "Bonemass: weak to Blunt and Frost (duplicate curated entry).",
         "source": "curated", "revid": 502, "url": "https://example.invalid/curated/bonemass-b"},
        # A curated record sharing a (title, heading) with a fandom/weirdgloop pair that WOULD
        # otherwise collapse (identical text) -- the curated record must neither be swept into,
        # nor block, that pair's own collapse-if-identical rule.
        {"title": "Fenring", "heading": "Weaknesses",
         "text": "Fenring is weak to fire.",
         "source": "fandom", "revid": 601, "url": "https://example.invalid/fandom/fenring"},
        {"title": "Fenring", "heading": "Weaknesses",
         "text": "Fenring is weak to fire.",
         "source": "weirdgloop", "revid": 602, "url": "https://example.invalid/weirdgloop/fenring"},
        {"title": "Fenring", "heading": "Weaknesses",
         "text": "Fenring (curated): weak to fire.",
         "source": "curated", "revid": 603, "url": "https://example.invalid/curated/fenring"},
    ]
    curated_kept = _dedupe(curated_passthrough_records)
    curated_kept_revids = sorted(r["revid"] for r in curated_kept)
    # Expected: both duplicate curated Bonemass entries survive (501, 502); the identical
    # fandom/weirdgloop Fenring pair collapses to the fandom copy per _dedupe()'s existing
    # "identical enough -- keep the commercial-clean licence" rule (602 dropped, 601 kept); the
    # curated Fenring entry survives untouched alongside that pair (603).
    curated_passthrough_ok = curated_kept_revids == [501, 502, 601, 603]
    print(f"{'PASS' if curated_passthrough_ok else 'FAIL'} curated records survive _dedupe() "
          f"untouched -- never collapsed against each other or folded into the fandom/weirdgloop "
          f"pairing logic, even sharing a (title, heading) key with either: kept revids="
          f"{curated_kept_revids!r} (want [501, 502, 601, 603])")
    all_ok = all_ok and curated_passthrough_ok

    curated_sources_ok = all(
        r["source"] == "curated" for r in curated_kept if r["revid"] in (501, 502, 603)
    )
    print(f"{'PASS' if curated_sources_ok else 'FAIL'} the surviving curated records keep "
          f"source == 'curated' through _dedupe() (never relabelled): {curated_sources_ok}")
    all_ok = all_ok and curated_sources_ok

    print("\n--- _rank_with_curated_boost(): comparable curated wins, weak curated never inverts ---")
    comparable_rows = [
        ("Bonemass", "Weaknesses", "wiki text", "weirdgloop", 1, "url-a", -10.0),
        ("Bonemass", "Curated: Boss Stats", "curated text", "curated", 2, "url-b", -9.5),
    ]
    boosted_comparable = _rank_with_curated_boost(comparable_rows)
    comparable_ok = boosted_comparable[0][3] == "curated"
    print(f"{'PASS' if comparable_ok else 'FAIL'} a curated row within "
          f"_CURATED_COMPARABLE_RATIO of the best raw bm25 (-9.5 vs best -10.0, ratio 0.95) is "
          f"promoted ahead of it: order={[r[3] for r in boosted_comparable]!r}")
    all_ok = all_ok and comparable_ok

    weak_vs_strong_rows = [
        ("Boar", "Weaknesses", "wiki text", "fandom", 3, "url-c", -10.0),
        ("Blueberries", "Curated: Food Stats", "curated text", "curated", 4, "url-d", -1.0),
    ]
    boosted_weak = _rank_with_curated_boost(weak_vs_strong_rows)
    weak_ok = boosted_weak[0][3] == "fandom"
    print(f"{'PASS' if weak_ok else 'FAIL'} a curated row far below the comparability ratio "
          f"(-1.0 vs best -10.0, ratio 0.10) is NOT promoted past a strongly-matching, "
          f"unrelated wiki row: order={[r[3] for r in boosted_weak]!r}")
    all_ok = all_ok and weak_ok

    print("\n--- curated boost + provenance, end-to-end through a REAL FTS5 index ---")
    with tempfile.TemporaryDirectory(prefix="wiki-index-selftest-curated-") as tmp_dir:
        records_path = os.path.join(tmp_dir, "wiki-records.jsonl")
        curated_db = os.path.join(tmp_dir, "wiki.db")
        e2e_records = [
            {"title": "Bonemass", "heading": "Weaknesses",
             "text": "Bonemass is weak to Blunt and Frost damage, and resistant to Slash damage.",
             "source": "weirdgloop", "revid": 701,
             "url": "https://example.invalid/weirdgloop/bonemass", "timestamp": ""},
            {"title": "Bonemass", "heading": "Curated: Boss Stats",
             "text": "Bonemass (3rd boss): weak to Blunt and Frost; resistant to Slash; "
                     "immune to Poison.",
             "source": "curated", "revid": 702,
             "url": "https://example.invalid/curated/bonemass", "timestamp": "",
             "provenance": {"wikis": ["fandom", "weirdgloop"],
                            "marker": "PROVENANCE_SHOULD_NOT_LEAK"}},
            {"title": "Boar", "heading": "Weaknesses",
             "text": "Boar boar boar is a creature found in the Meadows. Boar boar meat boar "
                     "boar boar tameable boar.",
             "source": "fandom", "revid": 703,
             "url": "https://example.invalid/fandom/boar", "timestamp": ""},
            {"title": "Blueberries", "heading": "Curated: Food Stats",
             "text": "Blueberries: 8 health, 25 stamina, 600s duration.",
             "source": "curated", "revid": 704,
             "url": "https://example.invalid/curated/blueberries", "timestamp": ""},
        ]
        with open(records_path, "w", encoding="utf-8") as fh:
            for rec in e2e_records:
                fh.write(json.dumps(rec) + "\n")

        # The 'provenance' field is extra to the schema _load_records() checks for -- confirm it
        # loads cleanly (not skipped as "missing a required field") and survives into memory for
        # _dedupe(), before build_index() ever gets to (correctly) leave it out of the DB.
        loaded_records, load_skipped = _load_records(records_path)
        bonemass_curated_loaded = next(
            (r for r in loaded_records if r.get("revid") == 702), None
        )
        provenance_load_ok = (
            load_skipped == 0
            and len(loaded_records) == 4
            and bonemass_curated_loaded is not None
            and "provenance" in bonemass_curated_loaded
        )
        print(f"{'PASS' if provenance_load_ok else 'FAIL'} a record carrying an extra "
              f"'provenance' field loads cleanly (0 skipped) and keeps the field in memory: "
              f"skipped={load_skipped}, loaded={len(loaded_records)}")
        all_ok = all_ok and provenance_load_ok

        build_index(records_path=records_path, db_path=curated_db)

        original_db_path = DB_PATH_DEFAULT
        try:
            DB_PATH_DEFAULT = curated_db
            _reset_cache_for_tests()

            comparable_result = search("bonemass weakness", k=2)
            comparable_e2e_ok = (
                len(comparable_result) == 2 and comparable_result[0]["source"] == "curated"
            )
            print(f"{'PASS' if comparable_e2e_ok else 'FAIL'} REAL index: the curated Bonemass "
                  f"record outranks the comparably-matching weirdgloop record: "
                  f"{[r['source'] for r in comparable_result]!r}")
            all_ok = all_ok and comparable_e2e_ok

            weak_vs_strong_result = search("boar stamina", k=2)
            weak_e2e_ok = (
                len(weak_vs_strong_result) == 2
                and weak_vs_strong_result[0]["source"] == "fandom"
            )
            print(f"{'PASS' if weak_e2e_ok else 'FAIL'} REAL index: a curated record that only "
                  f"weakly matches ('stamina') does NOT outrank a strongly-matching, unrelated "
                  f"fandom record ('boar' repeated): "
                  f"{[r['source'] for r in weak_vs_strong_result]!r}")
            all_ok = all_ok and weak_e2e_ok

            curated_only_result = search("blueberries", k=1)
            roundtrip_ok = (
                len(curated_only_result) == 1 and curated_only_result[0]["source"] == "curated"
            )
            print(f"{'PASS' if roundtrip_ok else 'FAIL'} source == 'curated' round-trips through "
                  f"build_index()/search() unchanged: {curated_only_result!r}")
            all_ok = all_ok and roundtrip_ok

            bonemass_curated_served = next(
                (r for r in comparable_result if r["source"] == "curated"), None
            )
            no_leak_ok = (
                bonemass_curated_served is not None
                and "provenance" not in bonemass_curated_served
                and "PROVENANCE_SHOULD_NOT_LEAK" not in bonemass_curated_served["text"]
            )
            served_keys = (
                sorted(bonemass_curated_served.keys())
                if bonemass_curated_served is not None else None
            )
            served_text = (
                bonemass_curated_served["text"] if bonemass_curated_served is not None else None
            )
            print(f"{'PASS' if no_leak_ok else 'FAIL'} the curated record's 'provenance' field "
                  f"does not leak into the served record or its text: "
                  f"keys={served_keys!r} text={served_text!r}")
            all_ok = all_ok and no_leak_ok
        finally:
            DB_PATH_DEFAULT = original_db_path
            _reset_cache_for_tests()

    print("\n--- English stopword stripping: the 'how much health does a Boar have' fix ---")
    # _strip_stopwords() unit-level checks -- see its own docstring and _STOPWORDS' module-level
    # comment for the corpus verification and the mechanism this locks in.
    filler_query_terms = [t.lower() for t in _TOKEN_RE.findall(
        "how much health does a Boar have"
    )]
    filler_stripped = _strip_stopwords(filler_query_terms)
    filler_stripped_ok = filler_stripped == ["health", "boar"]
    print(f"{'PASS' if filler_stripped_ok else 'FAIL'} interrogatives/auxiliaries/articles are "
          f"dropped from the real failing query, leaving only the entity terms: "
          f"{filler_stripped!r} (want ['health', 'boar'])")
    all_ok = all_ok and filler_stripped_ok

    all_stopword_terms = [t.lower() for t in _TOKEN_RE.findall("what is it")]
    all_stopword_stripped = _strip_stopwords(all_stopword_terms)
    never_empty_ok = (
        all_stopword_stripped == all_stopword_terms and len(all_stopword_stripped) > 0
    )
    print(f"{'PASS' if never_empty_ok else 'FAIL'} an all-stopword query ('what is it') falls "
          f"back to the ORIGINAL terms instead of being stripped to nothing: "
          f"{all_stopword_stripped!r}")
    all_ok = all_ok and never_empty_ok

    all_stopword_match_expr = _build_match_expression("what is it")
    all_stopword_match_expr_ok = bool(all_stopword_match_expr)
    print(f"{'PASS' if all_stopword_match_expr_ok else 'FAIL'} _build_match_expression('what is "
          f"it') still produces a searchable MATCH expression instead of None -- the query is "
          f"never dropped to nothing: {all_stopword_match_expr!r}")
    all_ok = all_ok and all_stopword_match_expr_ok

    try:
        all_stopword_search_result = search("what is it")
        all_stopword_search_ok = isinstance(all_stopword_search_result, list)
    except Exception as exc:
        all_stopword_search_result = exc
        all_stopword_search_ok = False
    print(f"{'PASS' if all_stopword_search_ok else 'FAIL'} search('what is it') does not raise "
          f"even though every one of its terms is a stopword: {all_stopword_search_result!r}")
    all_ok = all_ok and all_stopword_search_ok

    old_norse_terms = [t.lower() for t in _TOKEN_RE.findall("hvat er heilsa Boar")]
    old_norse_stripped = _strip_stopwords(old_norse_terms)
    old_norse_untouched_ok = old_norse_stripped == old_norse_terms
    print(f"{'PASS' if old_norse_untouched_ok else 'FAIL'} Old Norse query terms are untouched "
          f"by the (English-only) stopword set -- this phrasing already worked before the fix "
          f"and must keep working: {old_norse_stripped!r} (want unchanged {old_norse_terms!r})")
    all_ok = all_ok and old_norse_untouched_ok

    print("\n--- _STOPWORDS checked against the actual corpus -- never swallows a real entity ---")
    # This codifies the manual check performed before choosing _STOPWORDS (see its module-level
    # comment): every candidate word was looked up as an exact page title against BOTH live wikis
    # via MediaWiki's `action=query&titles=`, confirming none is a real entity. That was a
    # point-in-time check against a live service this test suite cannot re-run offline, so this
    # regression test instead locks the same property against everything checked into THIS repo
    # that names a real entity: every curated title (valheim-curated-numbers.jsonl) and every
    # ALIASES canonical value.
    #
    # The bar is "would this entity's name be fully erased", not "does any one token overlap" --
    # a multi-word name with ONE stopword-ish token (e.g. ALIASES' "The Elder", tokens ["the",
    # "elder"]) is unaffected by stripping "the": "elder" alone still finds it, so that overlap is
    # harmless by design (_strip_stopwords only ever REMOVES stopword tokens, it never touches a
    # non-stopword one). The only real danger is an entity name whose tokens are ALL stopwords --
    # then a query mentioning it alongside other real content would have every trace of that
    # name's own words stripped out from under it.
    entity_names = []
    curated_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "valheim-curated-numbers.jsonl"
    )
    try:
        with open(curated_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                entity_names.append(rec.get("title", ""))
    except FileNotFoundError:
        pass  # not guaranteed present in every environment this module runs in -- ALIASES
              # alone below still gives this check real content to compare against
    entity_names.extend(ALIASES.values())

    def _fully_stopword_names(stopwords, names):
        swallowed = []
        for name in names:
            name_tokens = [t.lower() for t in _TOKEN_RE.findall(name)]
            if name_tokens and all(t in stopwords for t in name_tokens):
                swallowed.append(name)
        return sorted(set(swallowed))

    swallowed_entities = _fully_stopword_names(_STOPWORDS, entity_names)
    corpus_collision_ok = not swallowed_entities
    print(f"{'PASS' if corpus_collision_ok else 'FAIL'} no real curated title or ALIASES "
          f"canonical value is made ENTIRELY of _STOPWORDS tokens (which would erase it "
          f"completely from any query): swallowed={swallowed_entities!r}")
    all_ok = all_ok and corpus_collision_ok

    # Prove that check isn't vacuous: it must actually catch a real-entity collision if one is
    # introduced. Simulated with "boar" -- the entity this entire bug report is about -- added to
    # a LOCAL COPY of _STOPWORDS (never mutating the real module state other tests rely on). Once
    # "boar" is (wrongly) a stopword, the single-token curated title "Boar" becomes entirely
    # stopwords and must be flagged.
    sabotaged_stopwords = _STOPWORDS | {"boar"}
    sabotaged_swallowed = _fully_stopword_names(sabotaged_stopwords, entity_names)
    collision_check_catches_real_entity_ok = sabotaged_swallowed == ["Boar"]
    print(f"{'PASS' if collision_check_catches_real_entity_ok else 'FAIL'} the check actually "
          f"detects it if a real entity name is added to the stopword set -- this is not a "
          f"vacuous lock: simulated by adding 'boar' to a COPY of _STOPWORDS, "
          f"swallowed={sabotaged_swallowed!r}")
    all_ok = all_ok and collision_check_catches_real_entity_ok

    print("\n--- English stopword fix, end-to-end through a REAL FTS5 index with realistic "
          "decoys ---")
    # Reproduces the actual live-deployment defect: a curated Boar record with the real HP
    # numbers exists and is reachable, but loses to food items ("Boar Jerky", "Boar Meat") and to
    # a generic "Health" page once a natural-language question brings enough English filler words
    # into the query for their sheer incidental prose-matching volume to outscore the short,
    # precisely-relevant curated record. The fandom "Boar" creature page and a "Poison >
    # Stacking" page are included as further decoys -- with this exact fixture, sabotaging the
    # fix (making _strip_stopwords a no-op) reproduces the exact observed wrong top result,
    # "[fandom] Poison > Stacking", for the first query below.
    with tempfile.TemporaryDirectory(prefix="wiki-index-selftest-stopwords-") as tmp_dir:
        records_path = os.path.join(tmp_dir, "wiki-records.jsonl")
        stopword_db = os.path.join(tmp_dir, "wiki.db")
        boar_records = [
            {"title": "Boar", "heading": "Curated: Creature Stats",
             "text": "Boar: 1-star 10 health, 2-star 20 health, 3-star 40 health. Deals 10 "
                     "blunt damage.",
             "source": "curated", "revid": 1001,
             "url": "https://example.invalid/curated/boar", "timestamp": ""},
            {"title": "Boar", "heading": "Overview",
             "text": "The Boar is a passive creature found in the Meadows biome. It can be "
                     "tamed and bred using Barley and Mushrooms. Wild Boars will attack if "
                     "provoked.",
             "source": "fandom", "revid": 1002,
             "url": "https://example.invalid/fandom/boar", "timestamp": ""},
            {"title": "Boar Jerky", "heading": "Food",
             "text": "Boar Jerky is a food item made from Boar Meat. It restores 25 health "
                     "and 60 stamina over a duration of 1200 seconds. Boar Jerky is crafted at "
                     "a Cooking Station using Boar Meat and salt.",
             "source": "fandom", "revid": 1003,
             "url": "https://example.invalid/fandom/boar-jerky", "timestamp": ""},
            {"title": "Boar Meat", "heading": "Raw Material",
             "text": "Boar Meat is a raw material dropped by killing a Boar. It can be cooked "
                     "over a campfire or used to craft Boar Jerky and other Boar meat dishes.",
             "source": "fandom", "revid": 1004,
             "url": "https://example.invalid/fandom/boar-meat", "timestamp": ""},
            {"title": "Health", "heading": "Overview",
             "text": "Health is a core survival stat. A player's health does decrease when "
                     "they take damage and does regenerate over time. How much health a "
                     "player has depends on food eaten. Health does not regenerate while a "
                     "player has negative stamina.",
             "source": "fandom", "revid": 1005,
             "url": "https://example.invalid/fandom/health", "timestamp": ""},
            {"title": "Poison", "heading": "Stacking",
             "text": "Poison damage does stack when a player is hit multiple times. How much "
                     "poison a creature has applied does increase over time and does not "
                     "reset until it has worn off completely.",
             "source": "fandom", "revid": 1006,
             "url": "https://example.invalid/fandom/poison-stacking", "timestamp": ""},
        ]
        with open(records_path, "w", encoding="utf-8") as fh:
            for rec in boar_records:
                fh.write(json.dumps(rec) + "\n")
        build_index(records_path=records_path, db_path=stopword_db)

        original_db_path = DB_PATH_DEFAULT
        try:
            DB_PATH_DEFAULT = stopword_db
            _reset_cache_for_tests()

            def top_source_title(query):
                r = search(query, k=1)
                return (r[0]["source"], r[0]["title"]) if r else None

            filler_query_result = top_source_title("how much health does a Boar have")
            filler_query_ok = filler_query_result == ("curated", "Boar")
            print(f"{'PASS' if filler_query_ok else 'FAIL'} REAL index: 'how much health does "
                  f"a Boar have' -- the main reported failure -- now returns the curated Boar "
                  f"record first instead of a food item or the generic Health page: "
                  f"{filler_query_result!r}")
            all_ok = all_ok and filler_query_ok

            star_level_result = top_source_title("Boar hitpoints star level")
            star_level_ok = star_level_result == ("curated", "Boar")
            print(f"{'PASS' if star_level_ok else 'FAIL'} REAL index: 'Boar hitpoints star "
                  f"level' (already correct pre-fix) still returns curated Boar first: "
                  f"{star_level_result!r}")
            all_ok = all_ok and star_level_ok

            bare_boar_result = top_source_title("Boar")
            bare_boar_ok = bare_boar_result == ("curated", "Boar")
            print(f"{'PASS' if bare_boar_ok else 'FAIL'} REAL index: the bare query 'Boar' "
                  f"(already correct pre-fix) still returns curated Boar first: "
                  f"{bare_boar_result!r}")
            all_ok = all_ok and bare_boar_ok

            old_norse_result = top_source_title("hvat er heilsa Boar")
            old_norse_ok = old_norse_result == ("curated", "Boar")
            print(f"{'PASS' if old_norse_ok else 'FAIL'} REAL index: the Old Norse phrasing "
                  f"'hvat er heilsa Boar' (already correct pre-fix, because its filler matches "
                  f"nothing) still returns curated Boar first: {old_norse_result!r}")
            all_ok = all_ok and old_norse_ok

            # NOTE on 'Boar health': tokenizes to ["boar", "health"] -- ZERO stopwords, so
            # _build_match_expression("Boar health") is byte-identical with or without this fix
            # (confirmed by temporarily making _strip_stopwords a no-op against this exact
            # fixture: the result did not change). It is asserted below as a regression check on
            # the current, already-correct ranking in THIS fixture (curated Boar's short
            # title/text wins the bm25 + curated-boost comparison against the longer food-item
            # decoys) -- NOT as a lock on the stopword fix, because no stopword-only change CAN
            # affect a query that contains no stopwords. See this session's report for why the
            # live 5,911-section index's "WRONG ORDER" result for this exact query is a separate
            # ranking issue that stopword stripping does not address.
            boar_health_result = top_source_title("Boar health")
            boar_health_ok = boar_health_result == ("curated", "Boar")
            print(f"{'PASS' if boar_health_ok else 'FAIL'} REAL index: 'Boar health' returns "
                  f"curated Boar first in this fixture -- NOT a lock on the stopword fix (this "
                  f"query has no stopwords to strip; see the comment above): "
                  f"{boar_health_result!r}")
            all_ok = all_ok and boar_health_ok
        finally:
            DB_PATH_DEFAULT = original_db_path
            _reset_cache_for_tests()

    print("\n--- search(): never raises -- missing db, empty query, no-match, FTS5 metachars ---")
    original_db_path = DB_PATH_DEFAULT
    try:
        with tempfile.TemporaryDirectory(prefix="wiki-index-selftest-") as tmp_dir:
            missing_db = os.path.join(tmp_dir, "does-not-exist.db")
            DB_PATH_DEFAULT = missing_db
            _reset_cache_for_tests()

            buf_missing = io.StringIO()
            with contextlib.redirect_stderr(buf_missing):
                missing_result = search("anything")
            missing_ok = missing_result == []
            print(f"{'PASS' if missing_ok else 'FAIL'} search() against a missing db returns []: "
                  f"{missing_result!r}")
            all_ok = all_ok and missing_ok
            missing_silent_ok = buf_missing.getvalue() == ""
            print(f"{'PASS' if missing_silent_ok else 'FAIL'} a missing db (no index built yet) "
                  f"is the normal, silent case -- logs nothing, unlike a broken one (H1): "
                  f"{buf_missing.getvalue()!r}")
            all_ok = all_ok and missing_silent_ok

            empty_query_result = search("")
            empty_query_ok = empty_query_result == []
            print(f"{'PASS' if empty_query_ok else 'FAIL'} search('') returns []: "
                  f"{empty_query_result!r}")
            all_ok = all_ok and empty_query_ok

            # A real, healthy index for the no-match / metacharacter / max_chars checks below.
            records_path = os.path.join(tmp_dir, "wiki-records.jsonl")
            healthy_db = os.path.join(tmp_dir, "wiki.db")
            filler = " ".join(["lengthy"] * 900)  # well over 4000 chars, no unbroken long run
            stub_records = [
                {"title": "Fenring", "heading": "Weaknesses",
                 "text": "Fenring is weak to fire and pierce damage.",
                 "source": "weirdgloop", "revid": 101,
                 "url": "https://example.invalid/weirdgloop/fenring", "timestamp": ""},
                {"title": "Serpent", "heading": "Drops",
                 "text": "Serpent drops these items: " + filler,
                 "source": "fandom", "revid": 301,
                 "url": "https://example.invalid/fandom/serpent", "timestamp": ""},
            ]
            with open(records_path, "w", encoding="utf-8") as fh:
                for rec in stub_records:
                    fh.write(json.dumps(rec) + "\n")
            build_index(records_path=records_path, db_path=healthy_db)
            DB_PATH_DEFAULT = healthy_db
            _reset_cache_for_tests()

            no_match_result = search("how many players are online today")
            no_match_ok = no_match_result == []
            print(f"{'PASS' if no_match_ok else 'FAIL'} search() against a healthy index with no "
                  f"matching terms returns []: {no_match_result!r}")
            all_ok = all_ok and no_match_ok

            metachar_query = "\"unterminated OR NEAR() AND fire\" NOT frost"
            try:
                metachar_result = search(metachar_query)
                metachar_ok = isinstance(metachar_result, list)
            except Exception as exc:
                metachar_result = exc
                metachar_ok = False
            print(f"{'PASS' if metachar_ok else 'FAIL'} search() with raw FTS5 metacharacters in "
                  f"the query does not raise: {metachar_result!r}")
            all_ok = all_ok and metachar_ok

            budget_result = search("serpent drops", max_chars=100)
            budget_total = sum(len(r["text"]) for r in budget_result)
            budget_ok = 0 < budget_total <= 100
            print(f"{'PASS' if budget_ok else 'FAIL'} max_chars is respected even when the best "
                  f"match alone is much longer than the budget: {budget_total} chars (limit 100)")
            all_ok = all_ok and budget_ok

            _reset_cache_for_tests()  # release the connection before TemporaryDirectory cleans up
    finally:
        DB_PATH_DEFAULT = original_db_path
        _reset_cache_for_tests()

    print("\n--- H1: a broken index logs an ERROR line; an honest empty result logs nothing ---")
    with tempfile.TemporaryDirectory(prefix="wiki-index-selftest-h1-") as tmp_dir:
        broken_db = os.path.join(tmp_dir, "broken.db")
        sqlite3.connect(broken_db).close()  # a valid sqlite file, but missing the `sections` table

        original_db_path = DB_PATH_DEFAULT
        try:
            DB_PATH_DEFAULT = broken_db
            _reset_cache_for_tests()
            _reset_error_log_for_tests()

            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                broken_result = search("anything")
            broken_ok = broken_result == []
            print(f"{'PASS' if broken_ok else 'FAIL'} search() against a table-less (broken) db "
                  f"still returns [] rather than raising: {broken_result!r}")
            all_ok = all_ok and broken_ok

            logged = buf.getvalue()
            logged_ok = bool(logged.strip()) and "no such table" in logged.lower()
            print(f"{'PASS' if logged_ok else 'FAIL'} the broken-index failure IS logged to "
                  f"stderr -- this is H1: it used to log NOTHING, identical to an honest "
                  f"no-match: {logged!r}")
            all_ok = all_ok and logged_ok

            buf2 = io.StringIO()
            with contextlib.redirect_stderr(buf2):
                search("something else entirely")
            not_flooded_ok = buf2.getvalue() == ""
            print(f"{'PASS' if not_flooded_ok else 'FAIL'} the SAME failure on a second question "
                  f"is rate-limited, not logged again immediately: {buf2.getvalue()!r}")
            all_ok = all_ok and not_flooded_ok
        finally:
            DB_PATH_DEFAULT = original_db_path
            _reset_cache_for_tests()
            _reset_error_log_for_tests()

    with tempfile.TemporaryDirectory(prefix="wiki-index-selftest-h1b-") as tmp_dir:
        records_path = os.path.join(tmp_dir, "wiki-records.jsonl")
        healthy_db = os.path.join(tmp_dir, "wiki.db")
        with open(records_path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "title": "Fenring", "heading": "Weaknesses", "text": "Fenring is weak to fire.",
                "source": "weirdgloop", "revid": 1,
                "url": "https://example.invalid/weirdgloop/fenring", "timestamp": "",
            }) + "\n")
        build_index(records_path=records_path, db_path=healthy_db)

        original_db_path = DB_PATH_DEFAULT
        try:
            DB_PATH_DEFAULT = healthy_db
            _reset_cache_for_tests()
            buf3 = io.StringIO()
            with contextlib.redirect_stderr(buf3):
                honest_empty = search("how many players are online today")
            honest_empty_ok = honest_empty == [] and buf3.getvalue() == ""
            print(f"{'PASS' if honest_empty_ok else 'FAIL'} an honest no-match against a HEALTHY "
                  f"index returns [] and logs nothing -- the property H1 protects, a legitimate "
                  f"empty result must stay silent: result={honest_empty!r} "
                  f"logged={buf3.getvalue()!r}")
            all_ok = all_ok and honest_empty_ok
        finally:
            DB_PATH_DEFAULT = original_db_path
            _reset_cache_for_tests()

    print()
    if not all_ok:
        print("SELFTEST FAILED -- see FAIL lines above", file=sys.stderr)
        return 1
    print("selftest: all assertions passed")
    return 0


# ---------------------------------------------------------------- CLI (for W4's timer / manual runs)
def _main(argv):
    parser = argparse.ArgumentParser(
        description="Build the Valheim wiki FTS5 search index from wiki-records.jsonl."
    )
    parser.add_argument(
        "records",
        nargs="?",
        default=RECORDS_PATH_DEFAULT,
        help="path to wiki-records.jsonl (default: %(default)s)",
    )
    parser.add_argument(
        "--db",
        default=DB_PATH_DEFAULT,
        help="output wiki.db path (default: %(default)s)",
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="run this module's stdlib-only, no-network selftest and exit (no wiki-records.jsonl "
             "or on-disk wiki.db required; see cmd_selftest())",
    )
    args = parser.parse_args(argv)
    if args.selftest:
        return cmd_selftest()
    try:
        stats = build_index(args.records, args.db)
    except Exception as exc:
        print("valheim-wiki-index: build failed: %s" % exc, file=sys.stderr)
        return 1
    print("valheim-wiki-index: built %s -- %s" % (args.db, stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
