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

Stdlib only: sqlite3, json, os, re, sys, difflib, threading, argparse. All standard library.
Imported directly by valheim-bot.py (via importlib, like valheim-medals.py, because of the
hyphenated filename) and valheim-bot.py must stay stdlib-only per PLAN-v6's hard rules -- so
this module must too.

All paths take an env override (VALHEIM_WIKI_ROOT), matching the VALHEIM_RESTART_ROOT /
valheim-medals.py convention, so this can run against fixtures without touching /var.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sqlite3
import sys
import threading


# ---------------------------------------------------------------- paths & config
WIKI_ROOT = os.environ.get("VALHEIM_WIKI_ROOT", "/var/lib/valheim-wiki").rstrip("/")
RECORDS_PATH_DEFAULT = os.path.join(WIKI_ROOT, "wiki-records.jsonl")
DB_PATH_DEFAULT = os.path.join(WIKI_ROOT, "wiki.db")

# BM25 column weights, in the same order as the FTS5 table's indexed columns below: title far
# above heading above text. Wiki titles ARE the entities (PLAN-v6 W2.3) -- a title hit
# ("Fenring") is almost always the right page even before looking at body text. Lower bm25()
# values rank better in SQLite; ORDER BY rank ASC puts the best match first.
_BM25_WEIGHTS = (10.0, 5.0, 1.0)  # title, heading, text

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


def _get_connection(db_path):
    """Return a live sqlite3 connection to db_path, reopening it whenever the file's mtime has
    changed since the last call (step 6: a refresh takes effect without restarting the bot).
    Returns None -- never raises -- if the file is missing or cannot be opened."""
    try:
        mtime = os.stat(db_path).st_mtime
    except OSError:
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
        except sqlite3.Error:
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
         "source": "weirdgloop" | "fandom", "revid": int, "url": str}
    Returns [] when the index is missing, unreadable, or nothing matches. Never raises."""
    try:
        return _search_impl(query, k, max_chars)
    except Exception:
        # Any failure here degrades to "no game knowledge for this question" -- per PLAN-v6,
        # [] is a normal outcome, not an error, and W3 falls back to the model's own (flagged
        # unverified) knowledge. A broken index must never take the whole bot down with it.
        return []


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
    sql = (
        "SELECT title, heading, text, source, revid, url, "
        "bm25(sections, ?, ?, ?) AS rank "
        "FROM sections WHERE sections MATCH ? "
        "ORDER BY rank LIMIT ?"
    )
    with _cache_lock:  # serialize access to the shared connection across caller threads
        rows = conn.execute(
            sql, (weight_title, weight_heading, weight_text, match_expr, k)
        ).fetchall()

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
# "weak to fire" and "weakness: fire" phrasings), then captures the short answer that follows up
# to the next clause boundary (the char class deliberately excludes ".", so "...fire.\nresistant"
# stops the capture at "fire" rather than swallowing the next sentence).
_RELATION_VALUE_RE = re.compile(
    r"\b(" + "|".join(
        sorted((re.escape(k) for k in _RELATION_KEYWORDS), key=len, reverse=True)
    ) + r")\b"
    r"\s*(?:to)?\s*:?\s*"
    r"([a-z][a-z0-9 ,/&'-]*)",
    re.IGNORECASE,
)


def _extract_relations(normalized_text):
    """Pull {relation: set(values)} pairs -- e.g. {"weakness": {"fire"}, "resistance": {"frost",
    "poison"}} -- out of wiki prose, however it phrases them: a label ("weakness: fire") or
    inline ("weak to fire"), a single value or a comma/and/slash/&-separated list.

    Best-effort and deliberately loose: this is only ever used to find REASONS to keep both
    copies in _sections_materially_differ(), never to decide anything is safe to collapse on its
    own, so an over-eager match here costs at most one harmless extra duplicate, never a hidden
    contradiction."""
    relations = {}
    for m in _RELATION_VALUE_RE.finditer(normalized_text):
        canonical = _RELATION_KEYWORDS[m.group(1).lower()]
        items = {
            item.strip(" .")
            for item in re.split(r",|\band\b|/|&", m.group(2).lower())
            if item.strip(" .")
        }
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
    args = parser.parse_args(argv)
    try:
        stats = build_index(args.records, args.db)
    except Exception as exc:
        print("valheim-wiki-index: build failed: %s" % exc, file=sys.stderr)
        return 1
    print("valheim-wiki-index: built %s -- %s" % (args.db, stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
