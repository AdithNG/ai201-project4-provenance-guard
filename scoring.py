"""Confidence scoring and attribution bands.

confidence is interpreted as AI-likelihood (0 = human, 1 = AI).
Bands (from planning.md): < 0.35 likely human, 0.35-0.65 uncertain, > 0.65 likely AI.

The text pipeline is a weighted 3-signal ensemble (stretch). The original 2-signal
combine is kept for reference and tests.
"""

LOW_THRESHOLD = 0.35
HIGH_THRESHOLD = 0.65

# Original 2-signal weights (pre-ensemble).
LLM_WEIGHT = 0.6
STYLO_WEIGHT = 0.4

# Ensemble weights (3 signals). LLM keeps the largest vote as the most reliable
# signal; lexical is smallest because absence of markers is weak evidence.
ENSEMBLE_LLM = 0.5
ENSEMBLE_STYLO = 0.3
ENSEMBLE_LEXICAL = 0.2

# Image pipeline (multimodal stretch).
IMAGE_META = 0.6
IMAGE_CAPTION = 0.4

# Spread above which the three signals are considered to disagree.
DISAGREEMENT_SPREAD = 0.4


def attribution_for(score):
    """Map an AI-likelihood score to one of three attribution categories."""
    if score < LOW_THRESHOLD:
        return "likely_human"
    if score > HIGH_THRESHOLD:
        return "likely_ai"
    return "uncertain"


def combine(llm_score, stylo_score):
    """Original 2-signal combine into one AI-likelihood confidence in 0..1.

    LLM is weighted higher because stylometrics is noisier on short or edge text.
    """
    confidence = LLM_WEIGHT * llm_score + STYLO_WEIGHT * stylo_score
    return round(confidence, 3)


def combine_ensemble(llm_score, stylo_score, lexical_score):
    """Weighted 3-signal ensemble. Returns (confidence, disagreement_flag).

    Conflict resolution: the weighted average pulls toward the higher-weighted
    signals; when the signals genuinely split, the result lands mid-range and the
    wide uncertain band reports it honestly. disagreement is True when the spread
    between the three scores is large, so a reviewer can see the split.
    """
    confidence = (
        ENSEMBLE_LLM * llm_score
        + ENSEMBLE_STYLO * stylo_score
        + ENSEMBLE_LEXICAL * lexical_score
    )
    spread = max(llm_score, stylo_score, lexical_score) - min(
        llm_score, stylo_score, lexical_score
    )
    return round(confidence, 3), spread >= DISAGREEMENT_SPREAD


def combine_image(meta_score, caption_score):
    """Combine the image metadata signal with the caption LLM signal."""
    confidence = IMAGE_META * meta_score + IMAGE_CAPTION * caption_score
    return round(confidence, 3)
