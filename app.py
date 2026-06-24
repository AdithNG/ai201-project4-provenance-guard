"""Provenance Guard API.

Milestone 3 scope: POST /submit (first signal + audit log) and GET /log.
Second signal + real confidence (M4), labels + appeals + rate limiting (M5)
build on this.
"""

import uuid

from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import db
from labels import generate_label
from scoring import combine
from signals import llm_signal, stylo_signal

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


@app.route("/submit", methods=["POST"])
@limiter.limit(SUBMIT_LIMITS)
def submit():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    creator_id = (data.get("creator_id") or "").strip()

    if not text:
        return jsonify({"error": "text is required"}), 400
    if not creator_id:
        return jsonify({"error": "creator_id is required"}), 400

    llm_score, rationale = llm_signal(text)
    stylo_score, stylo_details = stylo_signal(text)
    confidence = combine(llm_score, stylo_score)
    label = generate_label(confidence)

    content_id = str(uuid.uuid4())
    db.save_classification(
        {
            "content_id": content_id,
            "creator_id": creator_id,
            "text": text,
            "attribution": label["attribution"],
            "confidence": confidence,
            "llm_score": llm_score,
            "stylo_score": stylo_score,
            "status": "classified",
            "rationale": rationale,
            "stylo_details": stylo_details,
        }
    )

    return jsonify(
        {
            "content_id": content_id,
            "attribution": label["attribution"],
            "confidence": confidence,
            "llm_score": llm_score,
            "stylo_score": stylo_score,
            "label": label,
        }
    )


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


@app.route("/log", methods=["GET"])
def log():
    return jsonify({"entries": db.get_log()})


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
