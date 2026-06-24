"""Provenance Guard API.

Milestone 3 scope: POST /submit (first signal + audit log) and GET /log.
Second signal + real confidence (M4), labels + appeals + rate limiting (M5)
build on this.
"""

import uuid

from flask import Flask, jsonify, request

import db
from scoring import attribution_for
from signals import llm_signal

app = Flask(__name__)
db.init_db()

PLACEHOLDER_LABEL = "Label pending: full confidence scoring lands in Milestone 4."


@app.route("/submit", methods=["POST"])
def submit():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    creator_id = (data.get("creator_id") or "").strip()

    if not text:
        return jsonify({"error": "text is required"}), 400
    if not creator_id:
        return jsonify({"error": "creator_id is required"}), 400

    llm_score, rationale = llm_signal(text)

    # Milestone 3: attribution comes from signal 1 alone; confidence and label are
    # placeholders until the second signal and scoring arrive in Milestone 4.
    attribution = attribution_for(llm_score)
    confidence = llm_score

    content_id = str(uuid.uuid4())
    db.save_classification(
        {
            "content_id": content_id,
            "creator_id": creator_id,
            "text": text,
            "attribution": attribution,
            "confidence": confidence,
            "llm_score": llm_score,
            "stylo_score": None,
            "status": "classified",
            "rationale": rationale,
        }
    )

    return jsonify(
        {
            "content_id": content_id,
            "attribution": attribution,
            "confidence": confidence,
            "llm_score": llm_score,
            "label": PLACEHOLDER_LABEL,
        }
    )


@app.route("/log", methods=["GET"])
def log():
    return jsonify({"entries": db.get_log()})


if __name__ == "__main__":
    app.run(port=5000, debug=True)
