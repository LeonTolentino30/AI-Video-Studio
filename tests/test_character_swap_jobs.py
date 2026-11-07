"""Offline Flask ownership tests; never import app.py or load environment files."""

from contextlib import closing
from dataclasses import replace
from pathlib import Path
import secrets
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from flask import Flask, session

from character_swap.providers.base import CharacterEditRequest, CharacterSwapProvider
from character_swap.service import CharacterSwapService
from character_swap.store import JobStore, SafetyError
from character_swap.web import OWNER_SESSION_KEY, register_job_lookup


class ForbiddenProvider(CharacterSwapProvider):
    def __init__(self):
        self.calls = 0

    def submit(self, request):
        self.calls += 1
        raise AssertionError("Provider submission is forbidden")

    def get_status(self, task_id):
        self.calls += 1
        raise AssertionError("Provider polling is forbidden")


class JobLookupTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "jobs.db"
        self.store = JobStore(self.path)
        self.provider = ForbiddenProvider()
        self.service = CharacterSwapService(self.store, {"fake": self.provider}, {"fake": "offline-model"})
        self.app = self.make_app(secrets.token_hex(32))
        self.client = self.app.test_client()
        self.client.get("/character-swap", base_url="https://localhost")
        with self.client.session_transaction() as browser:
            self.owner = browser[OWNER_SESSION_KEY]
        self.request = CharacterEditRequest("private-source.mp4", "private-adult.png", "Private edit instructions")
        self.job = self.service.create_job("private-idempotency-key", "fake", self.request, 60,
                                           owner_id=self.owner)

    def make_app(self, key, https=True, service=None):
        app = Flask(__name__)
        app.config.update(TESTING=True, SECRET_KEY=key, CHARACTER_SWAP_HTTPS=https)
        @app.get("/character-swap", endpoint="character_swap")
        def page():
            return "Existing Character Swap page"
        register_job_lookup(app, lambda: service or self.service)
        return app

    def lookup(self, client=None, job_id=None):
        return (client or self.client).get(
            "/character-swap/jobs/" + (job_id or self.job.id), base_url="https://localhost"
        )

    def test_correct_owner_gets_allowlisted_local_job(self):
        result = self.lookup()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.get_json(), {
            "id": self.job.id, "provider": "fake", "model": "offline-model",
            "status": "created", "error_code": None, "error_message": None,
        })
        self.assertEqual(result.headers["Cache-Control"], "no-store")
        self.assertIn("Cookie", result.vary)

    def test_unknown_different_session_missing_session_and_ownerless_are_same_404(self):
        stranger = self.app.test_client()
        stranger.get("/character-swap", base_url="https://localhost")
        anonymous = self.app.test_client()
        legacy = self.service.create_job("legacy", "fake", self.request, 60)
        responses = [self.lookup(job_id="unknown"), self.lookup(stranger),
                     self.lookup(anonymous), self.lookup(job_id=legacy.id)]
        for result in responses:
            self.assertEqual(result.status_code, 404)
            self.assertEqual(result.get_json(), {"error": "Character Swap job not found"})
        self.assertIsNone(self.store.get(legacy.id).owner_id)
        with anonymous.session_transaction() as browser:
            self.assertNotIn(OWNER_SESSION_KEY, browser)

    def test_two_browser_sessions_receive_distinct_stable_random_ids(self):
        other = self.app.test_client()
        other.get("/character-swap", base_url="https://localhost")
        with other.session_transaction() as browser:
            self.assertRegex(browser[OWNER_SESSION_KEY], r"^[0-9a-f]{64}$")
            self.assertNotEqual(browser[OWNER_SESSION_KEY], self.owner)
        self.client.get("/character-swap", base_url="https://localhost")
        with self.client.session_transaction() as browser:
            self.assertEqual(browser[OWNER_SESSION_KEY], self.owner)

    def test_cookie_protections_and_explicit_http_development_opt_out(self):
        client = self.app.test_client()
        result = client.get("/character-swap", base_url="https://localhost")
        cookie = result.headers["Set-Cookie"]
        for attribute in ("HttpOnly", "SameSite=Lax", "Secure"):
            self.assertIn(attribute, cookie)
        local = self.make_app(secrets.token_hex(32), https=False)
        cookie = local.test_client().get("/character-swap").headers["Set-Cookie"]
        self.assertNotIn("; Secure", cookie)
        self.assertIn("HttpOnly", cookie)

    def test_missing_and_insecure_signing_keys_fail_closed(self):
        for key in (None, "", "short", "a" * 64, "0123456789abcdef" * 4,
                    "example-secret-key-that-must-never-be-used"):
            with self.subTest(key_type=type(key).__name__):
                app = self.make_app(key)
                client = app.test_client()
                self.assertEqual(self.lookup(client).status_code, 503)
                self.assertEqual(client.get("/character-swap").status_code, 200)
                self.assertEqual(app.secret_key, key)  # No replacement key generated.

    def test_forged_or_differently_signed_cookie_cannot_read_job(self):
        foreign_app = self.make_app(secrets.token_hex(32))
        with foreign_app.test_request_context():
            session[OWNER_SESSION_KEY] = self.owner
            cookie = foreign_app.session_interface.get_signing_serializer(foreign_app).dumps(dict(session))
        client = self.app.test_client()
        client.set_cookie("session", cookie)
        self.assertEqual(self.lookup(client).status_code, 404)

    def test_same_configured_key_survives_app_restart(self):
        cookie = self.client.get_cookie("session").value
        restarted = self.make_app(self.app.secret_key).test_client()
        restarted.set_cookie("session", cookie)
        self.assertEqual(self.lookup(restarted).status_code, 200)

    def test_response_redacts_private_fields_and_untrusted_errors(self):
        self.store.update(
            self.job, status="failed", provider_task_id="private-provider-task",
            video_url="https://example.com/private-result?token=private-token",
            prepared_json=self.store.encode_request(replace(self.request, video_uri="private-prepared-url")),
            error_code="private-error-code", error_message="private-credential-diagnostics",
        )
        result = self.lookup()
        self.assertEqual(set(result.get_json()), {"id", "provider", "model", "status", "error_code", "error_message"})
        self.assertEqual(result.get_json()["error_code"], "job_error")
        self.assertNotIn("private", result.get_data(as_text=True))
        self.assertNotIn(self.owner, result.get_data(as_text=True))

    def test_known_error_uses_static_public_message(self):
        self.store.update(self.job, status="failed", error_code="invalid_output_url", error_message="private detail")
        result = self.lookup().get_json()
        self.assertEqual(result["error_code"], "invalid_output_url")
        self.assertEqual(result["error_message"], "The completed edit has no valid video URL. Review required.")

    def test_unknown_stored_status_is_normalized(self):
        self.store.update(self.job, status="private-future-state")
        self.assertEqual(self.lookup().get_json()["status"], "unknown")

    def test_lookup_never_changes_job_or_calls_providers(self):
        before = self.store.get(self.job.id)
        self.lookup()
        self.lookup(job_id="unknown")
        self.assertEqual(self.store.get(self.job.id), before)
        self.assertEqual(self.provider.calls, 0)
        with self.assertRaises(SafetyError):
            self.service.submit(self.job.id)
        self.assertEqual(self.provider.calls, 0)

    def test_owner_is_immutable_and_idempotency_cannot_claim_or_share_jobs(self):
        with self.assertRaises(ValueError):
            self.store.update(self.job, owner_id=secrets.token_hex(32))
        with self.assertRaises(SafetyError):
            self.service.create_job("private-idempotency-key", "fake", self.request, 60,
                                    owner_id=secrets.token_hex(32))
        self.assertEqual(self.store.get(self.job.id).owner_id, self.owner)

    def test_invalid_owner_is_rejected_at_creation_and_lookup(self):
        with self.assertRaises(SafetyError):
            self.service.create_job("bad-owner", "fake", self.request, 60, owner_id="")
        for owner in (None, "", "malformed"):
            with self.assertRaises(SafetyError):
                self.service.get_owned_job(self.job.id, owner)

    def test_existing_schema_migration_leaves_legacy_jobs_ownerless(self):
        path = Path(self.temp.name) / "legacy.db"
        # Reconstruct the original table definition without its added column.
        with closing(sqlite3.connect(self.path)) as connection:
            schema = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'character_swap_jobs'"
            ).fetchone()[0]
        legacy_schema = schema.replace(", owner_id TEXT", "")
        self.assertNotEqual(schema, legacy_schema)
        with closing(sqlite3.connect(path)) as connection:
            connection.execute(legacy_schema)
            connection.execute("""
                INSERT INTO character_swap_jobs
                (id, idempotency_key, provider, model, request_json, status, estimated_units)
                VALUES ('legacy', 'legacy', 'fake', 'offline-model', ?, 'created', 60)
            """, (self.store.encode_request(self.request),))
            connection.commit()
        migrated = JobStore(path)
        self.assertIsNone(migrated.get("legacy").owner_id)
        with self.assertRaises(SafetyError):
            migrated.get_owned("legacy", self.owner)
        self.assertIsNone(JobStore(path).get("legacy").owner_id)


if __name__ == "__main__":
    unittest.main()
