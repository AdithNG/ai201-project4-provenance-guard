"""Confidence scoring and attribution bands.

confidence is interpreted as AI-likelihood (0 = human, 1 = AI).
Bands (from planning.md): < 0.35 likely human, 0.35-0.65 uncertain, > 0.65 likely AI.

Signal 2 (stylometrics) and the weighted combine land in Milestone 4. For now the
band helper is used on the single available signal.
"""

LOW_THRESHOLD = 0.35
HIGH_THRESHOLD = 0.65

LLM_WEIGHT = 0.6
STYLO_WEIGHT = 0.4


def attribution_for(score):
    """Map an AI-likelihood score to one of three attribution categories."""
    if score < LOW_THRESHOLD:
        return "likely_human"
    if score > HIGH_THRESHOLD:
        return "likely_ai"
    return "uncertain"
