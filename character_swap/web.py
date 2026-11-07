"""Read-only Flask integration; no provider SDKs, credentials, or networking."""

import re
import secrets

from flask import jsonify, request, session

from .store import SafetyError


OWNER_SESSION_KEY = "character_swap_owner_id"
PUBLIC_STATUSES = {
    "created", "authorized", "preparing", "prepared", "submitting", "pending",
    "running", "succeeded", "failed", "cancelled", "unknown",
    "preparation_unknown", "submission_unknown",
}
PUBLIC_ERRORS = {
    "invalid_output_url": "The completed edit has no valid video URL. Review required.",
    "task_failed": "The edit could not be completed.",
    "preparation_unknown": "Media preparation requires review.",
    "submission_unknown": "The submission outcome requires review.",
    "status_unavailable": "Task status is temporarily unavailable.",
}


def secure_signing_key(key) -> bool:
    """Reject missing, short, obvious placeholder, and repetitive keys.

    These checks cannot prove entropy. Deployments must supply a securely
    generated, persistent key (recommended: at least 32 random bytes).
    """
    if not isinstance(key, (str, bytes)):
        return False
    value = key.decode("utf-8", errors="replace") if isinstance(key, bytes) else key
    if len(value) < 32 or len(set(value)) < 12 or any(char.isspace() for char in value):
        return False
    if any(word in value.lower() for word in ("changeme", "password", "development", "your-secret", "example")):
        return False
    return not any(value == (value[:size] * (len(value) // size + 1))[:len(value)]
                   for size in range(1, len(value) // 2 + 1))


def register_job_lookup(app, service_factory):
    """Register with an injected local-only service factory.

    Missing/insecure keys disable this feature, not existing generation routes.
    Secure cookies default on; HTTP-only local development must opt out through
    server configuration. No key is generated or loaded by this module.
    """
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
    app.config["SESSION_COOKIE_SECURE"] = app.config.get("CHARACTER_SWAP_HTTPS", True) is not False
    # Do not accept older, potentially weak signing keys through fallback config.
    app.config["SECRET_KEY_FALLBACKS"] = None

    @app.before_request
    def establish_character_swap_session():
        if request.endpoint == "character_swap" and request.method == "GET" and secure_signing_key(app.secret_key):
            owner = session.get(OWNER_SESSION_KEY)
            if not isinstance(owner, str) or not re.fullmatch(r"[0-9a-f]{64}", owner):
                session[OWNER_SESSION_KEY] = secrets.token_hex(32)

    @app.get("/character-swap/jobs/<job_id>")
    def character_swap_job_lookup(job_id):
        if not secure_signing_key(app.secret_key):
            return jsonify(error="Secure session configuration is required."), 503
        owner = session.get(OWNER_SESSION_KEY)
        if not isinstance(owner, str) or not re.fullmatch(r"[0-9a-f]{64}", owner):
            return jsonify(error="Character Swap job not found"), 404
        try:
            job = service_factory().get_owned_job(job_id, owner)
        except SafetyError:
            return jsonify(error="Character Swap job not found"), 404
        error_code = job.error_code if job.error_code in PUBLIC_ERRORS else ("job_error" if job.error_code else None)
        return jsonify(
            id=job.id, provider=job.provider, model=job.model,
            status=job.status if job.status in PUBLIC_STATUSES else "unknown",
            error_code=error_code,
            error_message=PUBLIC_ERRORS.get(error_code, "The job requires review.") if error_code else None,
        )

    @app.after_request
    def protect_job_lookup_cache(response):
        if request.endpoint in {"character_swap_job_lookup", "character_swap"}:
            response.headers["Cache-Control"] = "no-store"
            response.vary.add("Cookie")
        return response
