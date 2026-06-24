"""Provenance Guard API.

Endpoints:
  POST /submit            classify text or image metadata, return label + scores
  POST /appeal            contest a decision, flip status to under_review
  GET  /log               structured audit log (newest first)
  GET  /verify/challenge  issue a verification challenge (provenance certificate)
  POST /verify            complete verification, earn Verified Human Creator credential
  GET  /analytics         detection-pattern, appeal-rate, and extra metrics (JSON)
  GET  /dashboard         simple HTML view of the analytics
"""

import secrets
import uuid

from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import db
from labels import generate_label
from scoring import combine_ensemble, combine_image
from signals import llm_signal, stylo_signal, lexical_signal, metadata_signal

app = Flask(__name__)
db.init_db()

# Rate limiting. See README for the reasoning behind these specific values.
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)
SUBMIT_LIMITS = "10 per minute;100 per day"

# In-memory store of outstanding verification challenges (prototype).
_CHALLENGES = {}
_CHALLENGE_PROMPT = (
    "Type the code below exactly, then write at least one original sentence (40+ "
    "characters) in your own words about why you create."
)


# --- Classification ----------------------------------------------------------

def _classify_text(text, creator_id, verified):
    """Run the 3-signal ensemble over text and persist the decision."""
    llm_score, rationale = llm_signal(text)
    stylo_score, stylo_details = stylo_signal(text)
    lexical_score, lexical_details = lexical_signal(text)
    confidence, disagreement = combine_ensemble(llm_score, stylo_score, lexical_score)
    label = generate_label(confidence, verified=verified)

    content_id = str(uuid.uuid4())
    db.save_classification(
        {
            "content_id": content_id,
            "creator_id": creator_id,
            "content_type": "text",
            "text": text,
            "attribution": label["attribution"],
            "confidence": confidence,
            "llm_score": llm_score,
            "stylo_score": stylo_score,
            "lexical_score": lexical_score,
            "status": "classified",
            "rationale": rationale,
            "stylo_details": stylo_details,
            "lexical_details": lexical_details,
            "disagreement": disagreement,
            "verified": verified,
        }
    )

    return {
        "content_id": content_id,
        "content_type": "text",
        "attribution": label["attribution"],
        "confidence": confidence,
        "signals": {
            "llm_score": llm_score,
            "stylo_score": stylo_score,
            "lexical_score": lexical_score,
            "disagreement": disagreement,
        },
        "label": label,
    }


def _classify_image(metadata, creator_id, verified):
    """Run the image-metadata pipeline and persist the decision."""
    caption = (metadata.get("caption") or "").strip()
    meta_score, meta_details = metadata_signal(metadata)
    if caption:
        caption_score, caption_rationale = llm_signal(caption)
    else:
        caption_score, caption_rationale = 0.5, "no caption provided"
    confidence = combine_image(meta_score, caption_score)
    label = generate_label(confidence, verified=verified)

    content_id = str(uuid.uuid4())
    db.save_classification(
        {
            "content_id": content_id,
            "creator_id": creator_id,
            "content_type": "image_metadata",
            "text": caption or "(no caption)",
            "attribution": label["attribution"],
            "confidence": confidence,
            "llm_score": caption_score,
            "stylo_score": None,
            "lexical_score": None,
            "status": "classified",
            "rationale": caption_rationale,
            "metadata_details": meta_details,
            "verified": verified,
        }
    )

    return {
        "content_id": content_id,
        "content_type": "image_metadata",
        "attribution": label["attribution"],
        "confidence": confidence,
        "signals": {
            "metadata_score": meta_score,
            "caption_score": caption_score,
            "metadata_details": meta_details,
        },
        "label": label,
    }


@app.route("/submit", methods=["POST"])
@limiter.limit(SUBMIT_LIMITS)
def submit():
    data = request.get_json(silent=True) or {}
    creator_id = (data.get("creator_id") or "").strip()
    content_type = (data.get("content_type") or "text").strip()

    if not creator_id:
        return jsonify({"error": "creator_id is required"}), 400

    verified = db.get_certificate(creator_id) is not None

    if content_type == "image_metadata":
        metadata = data.get("metadata") or {}
        if not isinstance(metadata, dict) or not metadata:
            return jsonify({"error": "metadata object is required for image_metadata"}), 400
        result = _classify_image(metadata, creator_id, verified)
    else:
        text = (data.get("text") or "").strip()
        if not text:
            return jsonify({"error": "text is required"}), 400
        result = _classify_text(text, creator_id, verified)

    if verified:
        result["certificate"] = db.get_certificate(creator_id)
    return jsonify(result)


# --- Appeals -----------------------------------------------------------------

@app.route("/appeal", methods=["POST"])
def appeal():
    data = request.get_json(silent=True) or {}
    content_id = (data.get("content_id") or "").strip()
    creator_reasoning = (data.get("creator_reasoning") or "").strip()

    if not content_id:
        return jsonify({"error": "content_id is required"}), 400
    if not creator_reasoning:
        return jsonify({"error": "creator_reasoning is required"}), 400

    updated = db.file_appeal(content_id, creator_reasoning)
    if updated is None:
        return jsonify({"error": "unknown content_id"}), 404

    return jsonify(
        {
            "content_id": content_id,
            "status": "under_review",
            "message": "Appeal received. This content is now under review.",
        }
    )


# --- Provenance certificate (stretch) ----------------------------------------

@app.route("/verify/challenge", methods=["GET"])
def verify_challenge():
    creator_id = (request.args.get("creator_id") or "").strip()
    if not creator_id:
        return jsonify({"error": "creator_id is required"}), 400

    challenge_id = str(uuid.uuid4())
    code = secrets.token_hex(3).upper()
    _CHALLENGES[challenge_id] = {"creator_id": creator_id, "code": code}
    return jsonify(
        {
            "challenge_id": challenge_id,
            "code": code,
            "creator_id": creator_id,
            "instructions": _CHALLENGE_PROMPT,
        }
    )


@app.route("/verify", methods=["POST"])
def verify():
    data = request.get_json(silent=True) or {}
    creator_id = (data.get("creator_id") or "").strip()
    challenge_id = (data.get("challenge_id") or "").strip()
    code = (data.get("code") or "").strip().upper()
    statement = (data.get("statement") or "").strip()

    challenge = _CHALLENGES.get(challenge_id)
    if challenge is None or challenge["creator_id"] != creator_id:
        return jsonify({"error": "invalid or expired challenge"}), 400
    if code != challenge["code"]:
        return jsonify({"error": "code does not match challenge"}), 400
    if len(statement) < 40 or statement.upper() == code:
        return jsonify({"error": "statement must be original and at least 40 characters"}), 400

    _CHALLENGES.pop(challenge_id, None)
    certificate_id = str(uuid.uuid4())
    cert = db.grant_certificate(certificate_id, creator_id, statement)
    return jsonify(
        {
            "message": "Verification complete. Verified Human Creator credential issued.",
            "certificate": cert,
        }
    )


# --- Audit log + analytics ---------------------------------------------------

@app.route("/log", methods=["GET"])
def log():
    return jsonify({"entries": db.get_log()})


@app.route("/analytics", methods=["GET"])
def analytics():
    return jsonify(db.get_analytics())


@app.route("/dashboard", methods=["GET"])
def dashboard():
    m = db.get_analytics()
    dp = m["detection_pattern"]
    html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Provenance Guard - Analytics</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 640px; margin: 40px auto; }}
    .card {{ border: 1px solid #ddd; border-radius: 8px; padding: 16px; margin: 12px 0; }}
    .num {{ font-size: 28px; font-weight: 700; }}
    .row {{ display: flex; justify-content: space-between; padding: 4px 0; }}
  </style>
</head>
<body>
  <h1>Provenance Guard Analytics</h1>
  <div class="card">
    <div class="num">{m['total_submissions']}</div>
    <div>total submissions</div>
  </div>
  <div class="card">
    <h3>Detection pattern</h3>
    <div class="row"><span>Likely AI</span><span>{dp['likely_ai']['count']} ({dp['likely_ai']['ratio']})</span></div>
    <div class="row"><span>Uncertain</span><span>{dp['uncertain']['count']} ({dp['uncertain']['ratio']})</span></div>
    <div class="row"><span>Likely human</span><span>{dp['likely_human']['count']} ({dp['likely_human']['ratio']})</span></div>
  </div>
  <div class="card">
    <div class="row"><span>Appeal rate</span><span class="num">{m['appeal_rate']}</span></div>
    <div class="row"><span>Average confidence</span><span class="num">{m['average_confidence']}</span></div>
    <div class="row"><span>Verified creators</span><span class="num">{m['verified_creators']}</span></div>
  </div>
</body>
</html>"""
    return html


@app.errorhandler(429)
def rate_limit_exceeded(error):
    return (
        jsonify(
            {
                "error": "rate limit exceeded",
                "limit": SUBMIT_LIMITS,
                "message": "Too many submissions. Please slow down and try again later.",
            }
        ),
        429,
    )


if __name__ == "__main__":
    app.run(port=5000, debug=True)
