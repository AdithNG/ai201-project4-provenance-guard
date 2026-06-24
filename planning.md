# Provenance Guard — Planning

A backend system a creative-sharing platform can plug in to: classify whether
submitted text was AI-generated or human-written, score confidence honestly,
surface a plain-language transparency label, and let creators appeal.

> **Status:** Milestone 1 (architecture & decisions). Milestone 2 expands this
> with the five spec questions, exact label text, thresholds, and the AI Tool Plan.

---

## Architecture

### Submission flow (the path one piece of text takes)

```
                        POST /submit
                   { text, creator_id }
                            |
                            v
                 +----------------------+
                 |   Flask submit route |  <-- rate limiter checks caller first
                 |   (validates input)  |      (429 if over limit)
                 +----------------------+
                            | raw text
              +-------------+-------------+
              | raw text                  | raw text
              v                           v
   +--------------------+      +-------------------------+
   | Signal 1: Groq LLM |      | Signal 2: Stylometric   |
   | classifier         |      | heuristics (pure Python)|
   | -> llm_score 0..1  |      | -> stylo_score 0..1     |
   +--------------------+      +-------------------------+
              | llm_score                 | stylo_score
              +-------------+-------------+
                            v
                 +----------------------+
                 |  Confidence scoring  |  combine -> confidence 0..1
                 |  (weighted combine)  |  + attribution category
                 +----------------------+
                            | confidence + attribution
                            v
                 +----------------------+
                 |  Label generator     |  confidence -> one of 3 label texts
                 +----------------------+
                            | label text
                            v
                 +----------------------+        +------------------+
                 |  Storage / Audit log |------->|  SQLite (content |
                 |  (write entry)       |        |  + audit tables) |
                 +----------------------+        +------------------+
                            |
                            v
        Response: { content_id, attribution, confidence,
                    llm_score, stylo_score, label }
```

### Appeal flow

```
                        POST /appeal
              { content_id, creator_reasoning }
                            |
                            v
                 +----------------------+
                 |  Flask appeal route  |  look up content_id
                 +----------------------+
                            | found
                            v
                 +----------------------+        +------------------+
                 |  Status update +     |------->|  SQLite: status  |
                 |  Audit log append    |        |  -> under_review |
                 +----------------------+        |  + appeal entry  |
                            |                    +------------------+
                            v
        Response: { content_id, status: "under_review",
                    message: "appeal received" }
```

**Narrative (submission):** A creator POSTs `text` + `creator_id` to `/submit`.
The rate limiter approves the caller, the request is validated, then the raw text
fans out to two independent detectors — a Groq LLM classifier (semantic signal)
and a pure-Python stylometric analyzer (structural signal). Their two scores are
combined into a single calibrated confidence and an attribution category, which the
label generator turns into plain-language text. The decision (with both signal
scores) is written to the SQLite audit log, and the full structured result is
returned to the caller.

**Narrative (appeal):** A creator who disputes a verdict POSTs the `content_id`
and their `creator_reasoning` to `/appeal`. The system looks up the original
decision, flips its status to `under_review`, appends an appeal entry to the audit
log next to the original classification, and confirms receipt. No automatic
re-classification — a human reviewer picks it up from the queue.

---

## Architecture Narrative — Components

| Component | Responsibility |
|-----------|----------------|
| **Flask app** | HTTP layer; routes `/submit`, `/appeal`, `/log`. |
| **Rate limiter** (Flask-Limiter) | Throttles `/submit` per client to prevent flooding. |
| **Input validation** | Rejects missing/empty `text` or `creator_id`. |
| **Signal 1 — Groq LLM classifier** | Asks `llama-3.3-70b-versatile` to judge human vs. AI; returns a 0–1 AI-likelihood. Semantic/holistic signal. |
| **Signal 2 — Stylometric heuristics** | Pure-Python statistics (sentence-length variance, type-token ratio, punctuation density). Structural signal. |
| **Confidence scorer** | Combines the two signal scores into one calibrated confidence + attribution category. |
| **Label generator** | Maps confidence → one of three plain-language transparency labels. |
| **Storage (SQLite)** | Persists each content decision and its status (`classified` / `under_review`). |
| **Audit log (SQLite)** | Structured record of every decision and appeal. |
| **Appeals handler** | Updates status, logs appeal beside the original decision. |

---

## Detection Signals (Milestone 1 decisions)

We use **two genuinely independent signals** — one semantic, one structural.

### Signal 1 — Groq LLM classifier (semantic)
- **Measures:** Whether the text *reads* as human or AI-generated, holistically —
  tone, coherence, the "too-polished/too-balanced" feel of generated prose.
- **Why it differs human vs. AI:** AI writing tends toward even register, hedged
  balance, and generic phrasing; the LLM has seen enough of both to judge gestalt.
- **Blind spot:** Unreliable on very short text; can be fooled by lightly-edited AI
  or by formal-but-human writing; non-deterministic and can hallucinate confidence.
  It is a *judgment*, not a measurement.

### Signal 2 — Stylometric heuristics (structural)
- **Measures:** Quantifiable variability in the prose — sentence-length variance,
  type-token ratio (vocabulary diversity), punctuation density.
- **Why it differs human vs. AI:** AI text is statistically more uniform (low
  sentence-length variance, smoother rhythm); human writing is more irregular.
- **Blind spot:** Pure structure, blind to meaning — a repetitive or simple-vocab
  *human* poem can look "AI-uniform," and a deliberately varied AI prompt can mimic
  human variance. Short texts give unstable statistics.

**Why the pair is strong:** one reads *meaning*, the other measures *form*. Their
blind spots don't overlap, so combining them is more informative than either alone.

---

## False-Positive Scenario (human work flagged as AI)

A false positive — calling a human's writing AI — is the worst outcome on a
creative platform. Trace:

1. A non-native-English creator submits formal, evenly-structured prose.
2. Stylometrics see low variance → high AI-likelihood. The LLM is also swayed by
   the formal register → both signals lean "AI."
3. **Confidence scoring must not over-commit:** when signals agree on "AI" but the
   text is short/formal, the score should land in the *uncertain* band rather than
   high-confidence AI. We deliberately bias the label toward "uncertain" near the
   boundary so we rarely assert "AI" about a human with high confidence.
4. **Label:** the user sees an honest, hedged "uncertain" label — not an accusation.
5. **Appeal:** the creator POSTs `/appeal` with their reasoning; status → `under_review`,
   appeal logged beside the original decision for a human reviewer.

This asymmetry (false positive ≫ worse than false negative) drives the thresholds
and label wording we finalize in Milestone 2.

---

## API Surface (the contract)

| Endpoint | Method | Accepts | Returns |
|----------|--------|---------|---------|
| `/submit` | POST | `{ text, creator_id }` | `{ content_id, attribution, confidence, llm_score, stylo_score, label }` |
| `/appeal` | POST | `{ content_id, creator_reasoning }` | `{ content_id, status: "under_review", message }` |
| `/log` | GET | — | `{ entries: [ ...structured audit entries... ] }` |

- `/submit` is rate-limited; returns `400` on bad input, `429` over limit.
- `/appeal` returns `404` if `content_id` is unknown.
- `/log` is open here for grading/documentation; would require auth in production.
