# News provider — minimum requirements (Faza 2, subpasul știri)

The news feed exists to give the decision layer *context that a trader could have had at
the bar close*. In replay/backtest this is only sound if the feed is **point-in-time
correct**: at decision time `as_of = bar_close`, the model may see a news revision **only
if that exact revision was both published and already ingested by us at or before
`as_of`**. Attaching *today's* news (or a later correction) to a *historical* snapshot is
look-ahead leakage and silently inflates backtest quality. These requirements make that
impossible by construction.

A candidate provider is acceptable only if it can satisfy **all** of the following.

## 1. Identity (stable, before dedup/revisions mean anything)
- Every event carries a **stable identity** `(source, external_id)` that does **not**
  change across corrections/updates of the same event.
- `source` names the origin feed; `external_id` is that source's durable event id.
- Identity must survive headline edits, re-categorization, and re-publication.

## 2. Deduplication
- The same event delivered more than once (retransmission, multiple endpoints) must
  collapse to **one** item per identity in the decision view.
- Dedup is on identity, **not** on headline text (headlines get edited).

## 3. Revisions (corrections without rewriting history)
- An update to an event is a **new revision** of the same identity, carrying a monotonic
  `revision` (or a `revised_at` we can order by), its **own** `publication_time`, and its
  **own** first-seen ingestion time.
- Revisions are **append-only**: a correction must never overwrite or delete the earlier
  revision. The decision view picks the **latest revision that was visible by `as_of`** —
  earlier decisions must still reproduce the earlier revision.

## 4. Publication time (valid time)
- Each revision has a `publication_time` = when *that revision* became public.
- Timezone-aware UTC. This is the "valid time" axis of the bitemporal model.

## 5. First-seen ingestion time (transaction time — the leakage guard)
- Each revision has `ingestion_time` (a.k.a. first-seen) = when **we** first received
  that revision. This is the "transaction time" axis.
- The as_of gate requires **both** `publication_time <= as_of` **and**
  `ingestion_time <= as_of`. A story published before the bar but that we only pulled
  afterwards was **not** knowable at the bar and must be excluded.
- For live capture we stamp `ingestion_time` at receipt. For historical replay the
  provider must supply a **defensible** first-seen per revision (see §6); if it cannot,
  its history is usable only as a weak prior, never as ground truth for timing.

## 6. Historical support (what "replay-grade" means)
- The provider must serve **historical** events across at least the full backtest window
  (target: several years for gold/XAUUSD macro news), returning each revision's **original**
  `publication_time` (not a re-stamped "now").
- It must expose a **point-in-time / as-of query** (or enough transaction-time metadata for
  us to reconstruct one) so we can ask "what was visible at time T". A feed that only
  returns the *current* state of each story is **not** replay-grade.
- Corrections history must be retained (see §3), not flattened to the latest version.

## 7. Storage & licensing rights (a hard gate, checked before any integration)
- We must have the **right to store** headlines + metadata durably for replay/backtest.
  Many news APIs forbid storage or cap retention — that disqualifies them for replay.
- Redistribution: we store internally only; no re-publishing. Confirm the ToS permits
  internal storage for research/trading signals.
- PII / content licensing: headlines only; no full-article bodies unless licensed.
- Record the license terms and retention limit alongside the provider config.

## Data model (implemented in `data_collector/news/base.py`)
`NewsItem(source, external_id, revision, publication_time, ingestion_time, headline,
impact, sentiment_for_gold)` with `identity == (source, external_id)`.
- `as_of_filter(items, as_of)` — raw no-look-ahead temporal filter (both times `<= as_of`).
- `as_of_view(items, as_of)` — the **decision view**: no-look-ahead **+ dedup by identity +
  latest revision visible by as_of**. This is what the packet builder consumes.

## Replay discipline (non-negotiable)
- Building a packet for a historical `as_of` must call `as_of_view(..., as_of=bar_close)`.
  Never attach the live "current" news list to a historical snapshot.
- The same identity may appear with different revisions across two different `as_of`s —
  that is correct and required, not a bug.
