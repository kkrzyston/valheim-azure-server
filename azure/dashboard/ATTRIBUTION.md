# Attribution

Hermóðr's game-knowledge corpus (`wiki-records.jsonl`, built by `valheim-wiki-ingest.py`;
indexed into `wiki.db` by `valheim-wiki-index.py`) is derived from two community-maintained
Valheim wikis. Both licences require attribution as a condition of reuse. This file is that
attribution — kept with the corpus, not buried in a code comment, so it survives independently
of any one script.

This is a legal-compliance document. If you are deciding whether to use this corpus (or
Western Skies, or anything built on top of it) commercially, **read the non-commercial section
below before anything else.**

## Valheim Wiki on Fandom

- Source: <https://valheim.fandom.com/>
- Licence: **CC BY-SA**

Verified 2026-09-14 against Fandom's own API:
`https://valheim.fandom.com/api.php?action=query&meta=siteinfo&siprop=rightsinfo&format=json`
returned:

```json
{"batchcomplete":"","query":{"rightsinfo":{"url":"https://www.fandom.com/licensing","text":"CC-BY-SA"}}}
```

Fandom's own `rightsinfo` does not state a version number, and Fandom's licensing page
(`www.fandom.com/licensing`) returned HTTP 403/402 to automated fetches, so the version could
not be independently confirmed beyond "CC-BY-SA" — this matches PLAN-v6's own Sources table,
which likewise gives Fandom's licence as "CC BY-SA" with no version. Treat that as the verified
fact; if a version-specific citation is ever needed, confirm it by hand from a rendered wiki page
footer before publishing one.

Attribution (per Fandom's licence): this corpus uses material from the Valheim Wiki on Fandom
(<https://valheim.fandom.com/>) and is licensed under the Creative Commons BY-SA license.

## Valheim Wiki on Weird Gloop

- Source: <https://valheim.weirdgloop.org/>
- Licence: **split by revision date**
  - Revisions **before 7 September 2026** — CC BY-SA 3.0 (<https://creativecommons.org/licenses/by-sa/3.0/>)
  - Revisions **on or after 7 September 2026** — CC BY-NC-SA 3.0, **NonCommercial**
    (<https://creativecommons.org/licenses/by-nc-sa/3.0/>)

Verified 2026-09-14 against two independent Weird Gloop sources:

1. The wiki's own API —
   `https://valheim.weirdgloop.org/api.php?action=query&meta=siteinfo&siprop=rightsinfo&format=json`
   returned:

   ```json
   {"batchcomplete":"","query":{"rightsinfo":{"url":"https://creativecommons.org/licenses/by-nc-sa/3.0/","text":"CC BY-NC-SA 3.0"}}}
   ```

   This is the wiki's *current* default licence only — the API has no field for a historical
   split, which is why source 2 below is the one that establishes the date.

2. Weird Gloop's network-wide licensing policy at <https://meta.weirdgloop.org/w/Licensing>
   (linked from the footer of <https://weirdgloop.org/>). Its per-wiki table has a row for
   "Valheim Wiki" (`valheim.weirdgloop.org`) that states, verbatim:

   > Revisions on this wiki prior to **7 September 2026** are licensed under Creative Commons
   > BY-SA 3.0. Revisions on and after this date are licensed under Creative Commons BY-NC-SA
   > 3.0. If a revision is a "derivative work" of a prior revision, it should be considered to
   > be re-licensed from its previous license to the license aforementioned unless the prior
   > license expressly disallows such re-licensing. In this case, the new revision is licensed
   > under the same license as the previous revision.

**This confirms the licence facts in PLAN-v6 exactly — split date 7 September 2026, CC BY-SA 3.0
before it, CC BY-NC-SA 3.0 on or after it. No discrepancy found.**

The same page's "Using wiki content" section gives the required attribution form. Its own
worked example (for a different Weird Gloop wiki) reads:

> This article uses material from the Gielinor article on the RuneScape Wiki and is licensed
> under the Creative Commons BY-NC-SA 3.0 license.

Adapted for this corpus:

Attribution (per Weird Gloop's licence): this corpus uses material from pages on the Valheim
Wiki on Weird Gloop (<https://valheim.weirdgloop.org/>). Material from revisions dated 7
September 2026 or later is licensed under the Creative Commons BY-NC-SA 3.0 license; material
from earlier revisions is licensed under the Creative Commons BY-SA 3.0 license.

Per PLAN-v6 (W1, step 5), every record in `wiki-records.jsonl` carries `source`, `revid`,
`timestamp` and `url`, so the licence and citation for any single fact can be traced back to its
exact source revision rather than asserted only at the corpus level.

## The non-commercial condition — read this before any commercial use

**Any text drawn from a Weird Gloop revision dated 7 September 2026 or later is licensed
NonCommercial (CC BY-NC-SA 3.0).** That is not a formality: it means this portion of the corpus
may not be used, redistributed, or built upon for a purpose that is primarily intended for or
directed toward commercial advantage or monetary compensation — including, without a separate
licence from Weird Gloop, folding it into a commercial product or service. Western Skies is
free-to-use today, but if that ever changes, the NC-tagged fraction of this corpus is exactly
the part that decision has to reckon with. Everything from Fandom, and everything from Weird
Gloop dated before 7 September 2026, carries no such restriction (CC BY-SA / CC BY-SA 3.0,
share-alike only).

Practical consequence for anyone maintaining this corpus: the dedupe rule in W2 already prefers
the Fandom copy of a page when both wikis agree, specifically because Fandom's licence is the
commercial-clean one. When a Weird Gloop-only or Weird-Gloop-disagreeing record is kept, its
`timestamp` is what determines which of the two Weird Gloop licences applies to it — check that
field, not just the source name, before treating any Weird Gloop record as share-alike-only.

## Share-alike, in either case

Both CC BY-SA and CC BY-NC-SA are share-alike licences: anything derived from this corpus and
redistributed must carry the same (or a compatible) licence, plus the attribution above. That
applies to `wiki-records.jsonl` and `wiki.db` themselves, and to any answer Hermóðr gives that
quotes or closely paraphrases the source text — which is why `valheim-bot.py` is required to
cite the page a game-knowledge answer came from (see PLAN-v6, W3).

## Fetched facts (for audit)

| Query | Result |
|---|---|
| `valheim.fandom.com/api.php?action=query&meta=siteinfo&siprop=rightsinfo` (2026-09-14) | `{"url":"https://www.fandom.com/licensing","text":"CC-BY-SA"}` |
| `valheim.weirdgloop.org/api.php?action=query&meta=siteinfo&siprop=rightsinfo` (2026-09-14) | `{"url":"https://creativecommons.org/licenses/by-nc-sa/3.0/","text":"CC BY-NC-SA 3.0"}` |
| `meta.weirdgloop.org/w/Licensing`, "Valheim Wiki" row (2026-09-14) | Split date **7 September 2026**; CC BY-SA 3.0 before, CC BY-NC-SA 3.0 on/after — matches PLAN-v6 |
| `www.fandom.com/licensing` (2026-09-14) | HTTP 403 (direct fetch) / HTTP 402 (WebFetch) — could not independently confirm a version number beyond "CC-BY-SA" |
