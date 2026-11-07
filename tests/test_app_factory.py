"""Exercise the actual application factory without production setup or I/O."""

import ast
from contextlib import closing
import importlib.util
from pathlib import Path
import secrets
import sqlite3
from unittest.mock import patch

import test_character_swap_jobs as ownership


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"


def import_safe_app():
    """Check import safety before executing any application module code."""
    tree = ast.parse(APP_PATH.read_text(encoding="utf-8-sig"))
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef)):
            continue
        if isinstance(node, ast.If) and ast.unparse(node.test) == "__name__ == '__main__'":
            continue
        raise AssertionError("Unexpected executable module-level initialization")
    # Patch setup functions, not sys.modules: native dependency caches must survive.
    with patch("dotenv.load_dotenv", side_effect=AssertionError("dotenv forbidden")) as load, \
         patch("runwayml.RunwayML", side_effect=AssertionError("real provider forbidden")) as client, \
         patch("sqlite3.connect", side_effect=AssertionError("DB access during import forbidden")), \
         patch("pathlib.Path.mkdir", side_effect=AssertionError("mkdir during import forbidden")):
        spec = importlib.util.spec_from_file_location("offline_studio_factory", APP_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        load.assert_not_called()
        client.assert_not_called()
    return module


class ForbiddenRunway:
    def __init__(self):
        self.calls = 0

    def __getattr__(self, name):
        self.calls += 1
        raise AssertionError("Runway calls are forbidden")


class ActualFactoryOwnershipTests(ownership.JobLookupTests):
    """Run every ownership regression against create_app(), including migration."""

    def make_app(self, key, https=True, service=None):
        module = import_safe_app()
        self.runway = ForbiddenRunway()
        config = {
            "TESTING": True, "MOCK_MODE": True, "SECRET_KEY": key,
            "CHARACTER_SWAP_HTTPS": https,
            "DATABASE_PATH": Path(self.temp.name) / "text-video.db",
            "CHARACTER_SWAP_DATABASE_PATH": self.path,
            "UPLOAD_FOLDER": Path(self.temp.name) / "uploads",
        }
        with patch.object(module.os, "getenv", side_effect=AssertionError("Environment access forbidden")), \
             patch("dotenv.load_dotenv", side_effect=AssertionError("dotenv forbidden")), \
             patch("runwayml.RunwayML", side_effect=AssertionError("real client forbidden")):
            app = module.create_app(config, load_environment=False, runway_client=self.runway,
                                    character_swap_service=service or self.service)
        return app

    def test_actual_import_is_safe(self):
        module = import_safe_app()
        self.assertTrue(callable(module.create_app))
        self.assertFalse(hasattr(module, "app"))

    def test_existing_route_urls_methods_and_endpoints_are_registered(self):
        expected = {
            ("/", "GET", "home"), ("/config", "GET", "get_config"),
            ("/character-swap", "GET", "character_swap"),
            ("/character-swap/upload", "POST", "upload_character_swap"),
            ("/character-swap/prepare", "POST", "prepare_character_swap"),
            ("/character-swap/generate", "POST", "generate_character_swap"),
            ("/character-swap/status/<task_id>", "GET", "check_character_swap_status"),
            ("/generate", "POST", "generate_video"),
            ("/status/<task_id>", "GET", "check_video_status"),
            ("/gallery", "GET", "get_gallery"),
            ("/gallery/<int:video_id>", "DELETE", "delete_video"),
            ("/character-swap/jobs/<job_id>", "GET", "character_swap_job_lookup"),
        }
        actual = {(rule.rule, method, rule.endpoint) for rule in self.app.url_map.iter_rules()
                  for method in rule.methods}
        self.assertTrue(expected <= actual)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.runway.calls, 0)

    def test_mock_generation_and_gallery_use_only_injected_database(self):
        result = self.client.post("/generate", json={"prompt": "Offline demo"})
        self.assertEqual(result.status_code, 200)
        task_id = result.get_json()["task_id"]
        self.assertEqual(self.client.get("/status/" + task_id).get_json()["status"], "SUCCEEDED")
        gallery = self.client.get("/gallery").get_json()["videos"]
        self.assertEqual(len(gallery), 1)
        self.assertEqual(gallery[0]["prompt"], "Offline demo")
        self.assertEqual(self.client.delete("/gallery/" + str(gallery[0]["id"])).status_code, 200)
        self.assertEqual(self.client.get("/gallery").get_json()["videos"], [])
        self.assertEqual(self.runway.calls, 0)

    def test_paid_character_swap_submission_remains_blocked(self):
        result = self.client.post("/character-swap/generate", json={
            "video_uri": "runway://offline-video", "photo_uri": "runway://offline-image", "prompt": "Edit"
        })
        self.assertEqual(result.status_code, 403)
        self.assertEqual(self.client.get("/config").get_json()["mode"], "demo")
        self.assertEqual(self.runway.calls, 0)

    def test_demo_preparation_blocks_even_existing_uploaded_files(self):
        upload_id = "a" * 32
        uploads = Path(self.app.config["UPLOAD_FOLDER"])
        # Local dummy bytes only: the guard must run before media preparation.
        (uploads / f"{upload_id}_video.mp4").write_bytes(b"offline-video-placeholder")
        (uploads / f"{upload_id}_photo.png").write_bytes(b"offline-image-placeholder")
        response = self.client.post("/character-swap/prepare", json={"upload_id": upload_id})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json(), {
            "error": "Character Swap preparation is disabled in demo mode. No external upload request was sent.",
            "mode": "demo",
        })
        self.assertEqual(self.runway.calls, 0)

    def test_direct_requests_cannot_override_demo_preparation_or_generation(self):
        direct_client = self.app.test_client()  # No page visit or confirmation dialog.
        for payload in ({}, {"upload_id": "b" * 32, "MOCK_MODE": False, "mode": "real"}, []):
            with self.subTest(payload=payload):
                response = direct_client.post(
                    "/character-swap/prepare?MOCK_MODE=false&mode=real",
                    json=payload, headers={"X-Mock-Mode": "false"},
                )
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.get_json()["mode"], "demo")
        response = direct_client.post("/character-swap/generate?MOCK_MODE=false", json={
            "video_uri": "runway://offline-video", "photo_uri": "runway://offline-photo",
            "prompt": "Edit", "MOCK_MODE": False,
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.runway.calls, 0)

    def test_http_development_session_can_retrieve_its_job(self):
        app = self.make_app(secrets.token_hex(32), https=False)
        client = app.test_client()
        response = client.get("/character-swap", base_url="http://localhost")
        self.assertNotIn("; Secure", response.headers["Set-Cookie"])
        with client.session_transaction() as browser:
            owner = browser[ownership.OWNER_SESSION_KEY]
        job = self.service.create_job("http-job", "fake", self.request, 60, owner_id=owner)
        self.assertEqual(client.get("/character-swap/jobs/" + job.id,
                                    base_url="http://localhost").status_code, 200)

    def test_factory_initializes_and_migrates_temporary_database(self):
        # Legacy table contains real-shaped metadata but no owner column.
        legacy_path = Path(self.temp.name) / "old-jobs.db"
        with closing(sqlite3.connect(self.path)) as connection:
            schema = connection.execute("SELECT sql FROM sqlite_master WHERE name='character_swap_jobs'").fetchone()[0]
        with closing(sqlite3.connect(legacy_path)) as connection:
            connection.execute(schema.replace(", owner_id TEXT", ""))
            connection.execute("""
                INSERT INTO character_swap_jobs
                (id, idempotency_key, provider, model, request_json, status, estimated_units, reserved_units)
                VALUES ('old', 'old', 'fake', 'offline-model', ?, 'pending', 60, 60)
            """, (self.store.encode_request(self.request),))
            connection.commit()
        module = import_safe_app()
        config = dict(self.app.config)
        config["CHARACTER_SWAP_DATABASE_PATH"] = legacy_path
        with patch.object(module.os, "getenv", side_effect=AssertionError("Environment access forbidden")):
            app = module.create_app(config, load_environment=False, runway_client=ForbiddenRunway())
        migrated = app.extensions["character_swap_local_service"].get_job("old")
        self.assertIsNone(migrated.owner_id)
        self.assertEqual(migrated.status, "pending")
        self.assertEqual(migrated.reserved_units, 60)
        self.assertEqual(migrated.request, self.request)
        client = app.test_client()
        client.get("/character-swap", base_url="https://localhost")
        self.assertEqual(client.get("/character-swap/jobs/old", base_url="https://localhost").status_code, 404)

    def test_testing_configuration_fails_before_setup_if_unsafe(self):
        module = import_safe_app()
        valid = {
            "TESTING": True, "DATABASE_PATH": Path(self.temp.name) / "safe.db",
            "UPLOAD_FOLDER": Path(self.temp.name) / "safe-uploads",
        }
        cases = [({}, False, None), ({"MOCK_MODE": False}, False, ForbiddenRunway()),
                 ({"DATABASE_PATH": "videos.db"}, False, ForbiddenRunway()),
                 ({"CHARACTER_SWAP_DATABASE_PATH": "videos.db"}, False, ForbiddenRunway()),
                 ({"UPLOAD_FOLDER": None}, False, ForbiddenRunway()),
                 ({}, True, ForbiddenRunway())]
        with patch("sqlite3.connect", side_effect=AssertionError("Unsafe DB setup")), \
             patch.object(module.os, "getenv", side_effect=AssertionError("Unsafe environment access")):
            for changes, load, fake in cases:
                with self.subTest(changes=changes):
                    with self.assertRaises(ValueError):
                        module.create_app({**valid, **changes}, load_environment=load, runway_client=fake)

    def test_separate_factory_instances_do_not_share_database_state(self):
        module = import_safe_app()
        apps = []
        for index in range(2):
            apps.append(module.create_app({
                "TESTING": True, "DATABASE_PATH": Path(self.temp.name) / f"isolated-{index}.db",
                "UPLOAD_FOLDER": Path(self.temp.name) / f"isolated-uploads-{index}",
            }, load_environment=False, runway_client=ForbiddenRunway()))
        first, second = (app.test_client() for app in apps)
        first.post("/generate", json={"prompt": "First app only"})
        self.assertEqual(len(first.get("/gallery").get_json()["videos"]), 1)
        self.assertEqual(second.get("/gallery").get_json()["videos"], [])

    def test_production_setup_contract_uses_only_mocked_environment_and_client(self):
        module = import_safe_app()
        values = {"MOCK_MODE": "true", "CHARACTER_SWAP_HTTPS": "false",
                  "FLASK_SECRET_KEY": secrets.token_hex(32), "RUNWAYML_API_SECRET": "offline-placeholder"}
        fake = ForbiddenRunway()
        with patch.object(module.os, "getenv", side_effect=lambda name, default=None: values.get(name, default)), \
             patch("dotenv.load_dotenv") as dotenv, \
             patch("runwayml.RunwayML", return_value=fake) as constructor:
            app = module.create_app({
                "DATABASE_PATH": Path(self.temp.name) / "production-shaped-test.db",
                "UPLOAD_FOLDER": Path(self.temp.name) / "production-shaped-test-uploads",
            })
        dotenv.assert_called_once_with()
        constructor.assert_called_once_with(api_key="offline-placeholder")
        self.assertIs(app.extensions["runway_client"], fake)
        self.assertFalse(app.config["SESSION_COOKIE_SECURE"])
        self.assertEqual(fake.calls, 0)
