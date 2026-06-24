# Provenance Guard — Planning

A backend system a creative-sharing platform can plug in to: classify whether
submitted text was AI-generated or human-written, score confidence honestly,
surface a plain-language transparency label, and let creators appeal.

> **Status:** Milestone 2 complete — architecture, the five spec questions, exact
> label text, thresholds, edge cases, and the AI Tool Plan are all below.
>
> **Core scoring decisions:** confidence = `0.6 * llm_score + 0.4 * stylo_score`,
> interpreted as AI-likelihood (0 = human, 1 = AI). Bands: `< 0.35` likely human,
> `0.35–0.65` uncertain, `> 0.65` likely AI. Label tone: neutral & informative.

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

## Signal Outputs & How They Combine

### Signal 1 output — `llm_score` (0..1)
Groq `llama-3.3-70b-versatile` is prompted to return a single AI-likelihood
between 0 and 1 plus a one-sentence rationale. We parse the number into
`llm_score` (1 = reads as AI). The rationale is stored for the audit log /
reviewer, not used in math. On parse failure or API error we fall back to
`llm_score = 0.5` (maximally uncertain) so a flaky API never produces a confident
accusation.

### Signal 2 output — `stylo_score` (0..1)
Pure-Python. Three sub-scores, each normalized to 0..1 (1 = AI-like), then weighted
inside the signal (burstiness is the most reliable, punctuation the least):

| Metric | Computation | Maps to AI when… | Sub-score formula |
|--------|-------------|------------------|-------------------|
| Burstiness | std-dev of sentence lengths (words) | low variance (uniform) | `sub1 = clamp((8 - stdev) / 8, 0, 1)` |
| Lexical diversity | type-token ratio = unique/total words | very high (little repetition) | `sub2 = clamp((ttr - 0.45) / 0.30, 0, 1)` |
| Punctuation density | marks / total words | clusters mid (~0.12); extremes read human | `sub3 = clamp(1 - abs(pd - 0.12) / 0.12, 0, 1)` |

```
stylo_score = 0.5 * sub1 + 0.3 * sub2 + 0.2 * sub3
```

Constants (8, 0.45, 0.30, 0.12, the inner weights) are starting values; Milestone 4
testing on the four reference inputs will tune them. Texts under ~3 sentences make
stylometrics unstable — see Edge Cases.

### Combining into one confidence
```
confidence = 0.6 * llm_score + 0.4 * stylo_score      # AI-likelihood, 0..1
```
LLM is weighted higher because stylometrics is noisier on short/edge text. The
returned `confidence` IS the AI-likelihood (matches the spec's sample log, where
`attribution: likely_ai` pairs with `confidence: 0.78`).

---

## Uncertainty Representation

`confidence` is the system's estimated probability the text is AI-generated.
**0.5 means "the system genuinely cannot tell"** — equal pull from both signals,
no verdict. The further from 0.5, the more the wording commits.

| confidence (AI-likelihood) | attribution | label variant |
|----------------------------|-------------|---------------|
| `< 0.35` | `likely_human` | High-confidence human |
| `0.35 – 0.65` | `uncertain` | Uncertain |
| `> 0.65` | `likely_ai` | High-confidence AI |

**Why a wide uncertain band (0.35/0.65):** on a creative platform, confidently
calling a human's work AI is the worst failure. A wide middle zone means borderline
cases get an honest "we're not sure" instead of an accusation. This directly
encodes the false-positive asymmetry.

**How we'll validate it's meaningful (Milestone 4):** run the four reference inputs
(clearly-AI, clearly-human, formal-human, lightly-edited-AI). Clearly-AI should
land `> 0.65`, clearly-human `< 0.35`, and the two borderline cases in `0.35–0.65`.
If a borderline case lands confident, we print both signal scores to find which one
is over-committing and retune the constants.

---

## Transparency Label Variants (exact text)

Plain language, no jargon, neutral tone. Each variant's *text* differs (not just a
number), and every variant states it's an automated estimate and points to appeal.
The numeric confidence is also returned in the API response for transparency.

**High-confidence AI** (`confidence > 0.65`):
> **AI: Likely AI-generated.** Our analysis found strong signs that this text was
> produced by an AI system rather than written by a person. This is an automated
> estimate, not a certainty, and detection can be wrong. If you believe you wrote
> this yourself, you can appeal this label.

**Uncertain** (`confidence between 0.35 and 0.65`):
> **?: Uncertain origin.** Our analysis could not confidently tell whether this text
> was written by a person or generated by AI. We are showing this honestly rather
> than guessing. The creator is welcome to add context or appeal.

**High-confidence human** (`confidence < 0.35`):
> **Human: Likely human-written.** Our analysis found this reads as human-written,
> with the natural variation typical of a person's writing. This is an automated
> estimate and not a guarantee of authorship.

---

## Appeals Workflow

- **Who can appeal:** the creator of the content (identified by `creator_id` on the
  original submission). No auth is enforced in this prototype, but conceptually the
  appeal belongs to the creator.
- **What they provide:** `content_id` (from the `/submit` response) and
  `creator_reasoning` — free text explaining why they believe the label is wrong.
- **What the system does on receipt:**
  1. Look up `content_id`; return `404` if unknown.
  2. Update that content's `status` from `classified` → `under_review`.
  3. Append an audit-log entry of type `appeal` carrying `content_id`,
     `creator_reasoning`, and a timestamp, stored beside the original decision (which
     keeps its confidence and both signal scores).
  4. Return a confirmation: `{ content_id, status: "under_review", message }`.
- **No automated re-classification** — a human picks it up.
- **What a reviewer sees in the queue** (all rows where `status = under_review`):
  the original text, attribution, confidence, both individual signal scores, the
  original timestamp, and the creator's reasoning — enough to judge the appeal.

---

## Anticipated Edge Cases

1. **Repetitive, simple-vocabulary human poem (e.g., a nursery-rhyme-style piece
   with refrains).** Refrains crush sentence-length variance *and* type-token ratio,
   so stylometrics scores it AI-uniform (high `sub1`, high `sub2`). The LLM may also
   waver. Risk: false-positive against a human. Mitigation: wide uncertain band +
   LLM weighted higher + appeal path. This is the canonical case the asymmetry
   protects against.

2. **Lightly-edited AI text** (creator regenerates with AI, then rewrites a few
   sentences). The human edits add just enough burstiness to pull stylometrics
   toward "human," while the LLM may still detect AI phrasing — the signals disagree
   and the result lands in the uncertain band. That's the honest outcome; we don't
   pretend to resolve it.

3. **Very short submissions (a haiku, a tweet-length note).** With 1–2 sentences,
   sentence-length variance and TTR are statistically meaningless and the LLM is
   unreliable on little text. Plan: treat texts under ~3 sentences as low-evidence —
   nudge toward the uncertain band rather than asserting a verdict.

---

## AI Tool Plan

For each implementation milestone: which spec sections + the Architecture diagram go
in as input, what we ask the AI to generate, and how we verify before wiring it in.

**Milestone 3 — submission endpoint + first signal**
- *Provide:* `## Architecture` diagram + Detection Signals + Signal Outputs (Signal 1)
  + API Surface.
- *Ask for:* Flask app skeleton with the `POST /submit` route stub (returning a
  hardcoded response first), and the Groq signal function returning `llm_score`
  (0..1) + rationale.
- *Verify:* call the signal function directly on 2–3 inputs — confirm it returns a
  number in 0..1 and degrades to 0.5 on error; confirm `/submit` returns
  `content_id` + placeholders matching the API contract before adding logic.

**Milestone 4 — second signal + confidence scoring**
- *Provide:* Detection Signals + Signal Outputs (Signal 2 + combining) + Uncertainty
  Representation + diagram.
- *Ask for:* the stylometric function (3 metrics → `stylo_score`) and the combine +
  band-mapping logic (`0.6/0.4`, thresholds `0.35/0.65`).
- *Verify:* confirm the generated thresholds exactly match this doc (AI tools often
  drift); run the four reference inputs and check clearly-AI `>0.65`,
  clearly-human `<0.35`, borderline in the middle; print individual signal scores if
  not.

**Milestone 5 — production layer**
- *Provide:* Transparency Label Variants + Appeals Workflow + Rate-limit intent +
  diagram.
- *Ask for:* the label generator (confidence → the three exact texts above), the
  `POST /appeal` endpoint, and the Flask-Limiter config.
- *Verify:* all three label variants are reachable from real submissions; an appeal
  flips `status` to `under_review` and appears in `/log`; rapid requests over the
  limit return `429`.

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
