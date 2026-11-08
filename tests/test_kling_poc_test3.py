"""Offline checks for the exact reviewed Test 3 configuration."""

import unittest
import json
import io
from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import character_swap.kling_poc_test3 as test3
from character_swap.kling_poc import CONFIRMATION
from character_swap.poc_media import digest, validate_media
from character_swap.store import SafetyError
import test_kling_poc as fixtures

from character_swap.kling_poc_test3 import (
    EDIT_PROMPT, EXPERIMENT_ID, REFERENCE_FILENAMES,
)


class Test3ConfigurationTests(unittest.TestCase):
    def test_exact_complete_prompt(self):
        expected = "Edit <<<video_1>>>. Replace only the main performer with the adult in <<<image_1>>>, including their face, body, hairstyle, and clothing. <<<image_2>>> shows the same adult from another angle and expression; use it only as an additional facial identity reference. Keep the clothing and overall appearance from <<<image_1>>>. Preserve the original choreography, body movements, camera angles and camera movement, background and environment, lighting, supporting characters, other objects, and timing. Preserve the original audio. Additional editing instructions: The main performer is at the center of the frame."
        self.assertEqual(EDIT_PROMPT, expected)
        self.assertLessEqual(len(EDIT_PROMPT), 2500)

    def test_separate_experiment_and_reference_order(self):
        self.assertEqual(EXPERIMENT_ID, "kling-omni-pro-5s-v3")
        self.assertEqual(REFERENCE_FILENAMES, ("test1.png", "test2.jpeg"))


class TwoImageExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.video = self.root / "source.mp4"
        self.first, self.second = [self.root / name for name in REFERENCE_FILENAMES]
        for path, content in ((self.video, b"video"), (self.first, b"neutral"), (self.second, b"smiling")):
            path.write_bytes(content)
        self.urls = ["https://example.com/" + name for name in ("video", "first", "second")]
        self.transport = fixtures.FakeTransport()
        self.downloader = fixtures.FakeDownloader(dict(zip(self.urls, [b"video", b"neutral", b"smiling"])))
        self.validator = lambda video, image: {"video_sha256": digest(video), "image_sha256": digest(image), "duration_seconds": 5}
        replacement = patch.object(test3, "VIDEO_SHA256", digest(self.video))
        replacement.start()
        self.addCleanup(replacement.stop)

    def runner(self, enabled=False, **kwargs):
        return test3.Test3Runner(self.root / "test3", enabled=enabled, transport=self.transport,
                                downloader=self.downloader, validator=kwargs.get("validator", self.validator))

    def plan(self, runner):
        return runner.plan(self.video, self.first, *self.urls[:2],
                           second_reference=self.second, second_image_url=self.urls[2])

    def submit(self, runner):
        return runner.submit(CONFIRMATION, accept_estimated_billing=True)

    def test_exact_payload_and_persistent_two_image_manifest(self):
        job = self.plan(self.runner())
        resumed = self.runner()
        self.assertEqual(self.plan(resumed).id, job.id)
        payload = resumed.service.providers["kling"].build_payload(resumed.status().request)
        self.assertEqual(payload["prompt"], EDIT_PROMPT)
        self.assertEqual(payload["image_list"], [{"image_url": uri} for uri in self.urls[1:]])
        self.assertEqual(payload["video_list"], [{"video_url": self.urls[0], "refer_type": "base", "keep_original_sound": "yes"}])
        self.assertEqual((payload["mode"], payload["sound"], payload["multi_shot"]), ("pro", "off", False))
        record = json.loads(resumed.manifest.read_text())
        self.assertEqual(record["second_media"]["image_sha256"], digest(self.second))
        self.assertEqual(job.idempotency_key, EXPERIMENT_ID)

    def test_offline_and_missing_confirmation_never_fetch_or_post(self):
        self.plan(self.runner())
        with self.assertRaises(SafetyError):
            self.submit(self.runner())
        with self.assertRaises(SafetyError):
            self.runner(True).submit()
        self.assertEqual(self.downloader.calls + self.transport.posts, [])
        self.assertEqual(self.runner().status().reserved_units, 0)

    def test_both_remote_images_verified_then_one_post_across_restarts(self):
        self.plan(self.runner())
        result = self.submit(self.runner(True))
        self.assertEqual(result.reserved_units, 6000)
        self.submit(self.runner(True))
        self.assertEqual(len(self.transport.posts), 1)
        self.assertEqual([call[0] for call in self.downloader.calls], self.urls)

    def test_ambiguous_timeout_never_reposts(self):
        self.plan(self.runner())
        self.transport.error = TimeoutError()
        self.assertEqual(self.submit(self.runner(True)).status, "submission_unknown")
        self.submit(self.runner(True))
        self.assertEqual(len(self.transport.posts), 1)
        self.assertEqual(self.runner().status().reserved_units, 6000)

    def test_changed_local_second_image_blocks_before_network(self):
        self.plan(self.runner())
        self.second.write_bytes(b"changed")
        with self.assertRaises(SafetyError):
            self.submit(self.runner(True))
        self.assertEqual(self.downloader.calls + self.transport.posts, [])

    def test_mismatched_hosted_image_blocks_before_authorization(self):
        self.plan(self.runner())
        for url in self.urls[1:]:
            original = self.downloader.contents[url]
            self.downloader.contents[url] = b"wrong"
            with self.subTest(url=url), self.assertRaises(SafetyError):
                self.submit(self.runner(True))
            self.downloader.contents[url] = original
        self.assertEqual(self.transport.posts, [])
        self.assertEqual(self.runner().status().reserved_units, 0)

    def test_replanning_different_second_url_is_rejected(self):
        self.plan(self.runner())
        self.urls[2] += "changed"
        with self.assertRaises(SafetyError):
            self.plan(self.runner())

    def corrected_plan(self, runner):
        return runner.plan(self.video, self.first, *self.urls[:2], second_reference=self.second,
                           second_image_url=self.urls[2], new_plan=True)

    def test_corrected_plan_preserves_history_and_gets_unique_persistent_key(self):
        runner = self.runner()
        old = self.plan(runner)
        old_manifest = runner.manifest.read_bytes()
        old_encoded = runner.service.store.encode_request(old.request)
        self.urls[2] += "?corrected"
        new = self.corrected_plan(runner)
        self.assertNotEqual(new.id, old.id)
        self.assertNotEqual(new.idempotency_key, old.idempotency_key)
        self.assertTrue(new.idempotency_key.startswith(EXPERIMENT_ID + ":plan:"))
        saved_old = runner.service.get_job(old.id)
        self.assertEqual(saved_old.status, "superseded")
        self.assertEqual(runner.service.store.encode_request(saved_old.request), old_encoded)
        self.assertEqual((saved_old.reserved_units, new.reserved_units), (0, 0))
        self.assertEqual((runner.workspace / ("experiment-" + old.id + ".json")).read_bytes(), old_manifest)
        self.assertEqual(self.runner().status().id, new.id)
        self.assertEqual(self.plan(self.runner()).id, new.id)
        self.assertEqual(new.request.prompt, EDIT_PROMPT)
        self.assertEqual(self.transport.posts + self.downloader.calls, [])

    def test_superseded_plan_cannot_be_authorized_or_submitted(self):
        old = self.plan(self.runner())
        new = self.corrected_plan(self.runner())
        live = self.runner(True)
        self.assertEqual(live.service.authorize(old.id).status, "superseded")
        self.assertEqual(live.service.submit(old.id).status, "superseded")
        self.assertEqual(self.transport.posts, [])
        self.submit(live)
        self.submit(self.runner(True))
        self.assertEqual(len(self.transport.posts), 1)
        self.assertEqual(self.transport.posts[0][1]["external_task_id"], new.idempotency_key)

    def test_new_plan_blocks_all_reserved_or_attempted_states(self):
        for state in ("authorized", "submitting", "submission_unknown", "pending", "succeeded", "failed"):
            with self.subTest(state=state):
                runner = test3.Test3Runner(self.root / state, validator=self.validator)
                job = self.plan(runner)
                runner.service.store.update(job, status=state)
                before = runner.manifest.read_bytes()
                with self.assertRaises(SafetyError):
                    self.corrected_plan(runner)
                self.assertEqual(runner.manifest.read_bytes(), before)
        runner = self.runner(True)
        job = self.plan(runner)
        runner.service.authorize(job.id)
        with self.assertRaises(SafetyError):
            self.corrected_plan(runner)

    def test_new_plan_requires_existing_manifest(self):
        with self.assertRaises(SafetyError):
            self.corrected_plan(self.runner())

    def test_concurrent_replacements_only_create_one_new_job(self):
        runner = self.runner()
        old = self.plan(runner)
        def replace(_):
            try:
                return runner.service.store.replace_unsubmitted_plan(old.id, old.request).id
            except SafetyError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(replace, range(2)))
        self.assertEqual(sum(value is not None for value in results), 1)
        self.assertEqual(runner.service.get_job(old.id).status, "superseded")

    def test_multiple_unpaid_revisions_preserve_all_jobs_and_budget(self):
        runner = self.runner()
        first = self.plan(runner)
        second = self.corrected_plan(runner)
        third = self.corrected_plan(runner)
        self.assertEqual(len({job.idempotency_key for job in (first, second, third)}), 3)
        self.assertEqual([runner.service.get_job(job.id).status for job in (first, second, third)],
                         ["superseded", "superseded", "created"])
        self.submit(self.runner(True))
        with self.assertRaises(SafetyError):
            self.corrected_plan(self.runner())
        self.assertEqual(len(self.transport.posts), 1)

    def test_interrupted_manifest_publication_cannot_create_another_plan_or_post(self):
        runner = self.runner()
        old = self.plan(runner)
        with patch("character_swap.kling_poc_test3.os.replace", side_effect=OSError("offline failure")):
            with self.assertRaises(OSError):
                self.corrected_plan(runner)
        self.assertEqual(runner.service.get_job(old.id).status, "superseded")
        with self.assertRaises(SafetyError):
            self.corrected_plan(self.runner())
        self.assertEqual(self.submit(self.runner(True)).status, "superseded")
        self.assertEqual(self.transport.posts, [])

    def test_cli_new_plan_uses_corrected_urls_without_network(self):
        old = self.plan(self.runner())
        self.urls[2] += "?corrected"
        real_class = test3.Test3Runner
        with patch.object(test3, "Test3Runner", side_effect=lambda **kwargs: real_class(
                self.root / "test3", validator=self.validator, **kwargs)), \
             patch("character_swap.kling_poc.masked_input", side_effect=self.urls), \
             redirect_stdout(io.StringIO()):
            result = test3.main(["plan", "--new-plan", "--video", str(self.video),
                                 "--reference", str(self.first), "--reference-2", str(self.second)])
        self.assertEqual(result, 0)
        self.assertNotEqual(self.runner().status().id, old.id)
        self.assertEqual(self.transport.posts + self.downloader.calls, [])

    def test_missing_second_reference_and_reversed_order_rejected(self):
        runner = self.runner()
        with self.assertRaises(SafetyError):
            runner.plan(self.video, self.first, *self.urls[:2])
        with self.assertRaises(SafetyError):
            runner.plan(self.video, self.second, *self.urls[:2], second_reference=self.first, second_image_url=self.urls[2])
        self.assertFalse(runner.manifest.exists())

    def test_previous_workspaces_are_rejected(self):
        for workspace in (test3.TEST1_WORKSPACE, test3.TEST2_WORKSPACE):
            with self.assertRaises(SafetyError):
                test3.Test3Runner(workspace)

    def test_cli_plan_masks_three_urls_and_uses_exact_prompt(self):
        real_class = test3.Test3Runner
        with patch.object(test3, "Test3Runner", side_effect=lambda **kwargs: real_class(
                self.root / "test3", validator=self.validator, **kwargs)), \
             patch("character_swap.kling_poc.masked_input", side_effect=self.urls), \
             redirect_stdout(io.StringIO()) as output:
            result = test3.main(["plan", "--video", str(self.video), "--reference", str(self.first),
                                 "--reference-2", str(self.second)])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["experiment_id"], EXPERIMENT_ID)
        self.assertEqual(self.runner().status().request.prompt, EDIT_PROMPT)

    def test_concurrent_submit_claims_only_one_paid_post(self):
        self.plan(self.runner())
        runners = [self.runner(True), self.runner(True)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(self.submit, runners))
        self.assertEqual(len(self.transport.posts), 1)
        self.assertEqual(self.runner().status().reserved_units, 6000)

    def test_poll_resumes_saved_task_without_another_post(self):
        self.plan(self.runner())
        self.submit(self.runner(True))
        self.transport.response = {"code": 0, "data": {
            "task_id": "offline-task", "task_status": "processing"}}
        self.assertEqual(self.runner(True).poll().status, "running")
        self.assertEqual(len(self.transport.posts), 1)
        self.assertEqual(len(self.transport.gets), 1)

    def test_second_image_uses_real_format_dimension_ratio_and_size_validation(self):
        self.video.write_bytes(b"\x00\x00\x00\x18ftypisomoffline")
        self.first.write_bytes(b"\x89PNG\r\n\x1a\noffline")
        self.second.write_bytes(b"\xff\xd8\xffoffline")
        with patch.object(test3, "VIDEO_SHA256", digest(self.video)):
            for defect in (None, "dimensions", "ratio", "format", "size"):
                def validator(video, image):
                    second = Path(image).suffix == ".jpeg"
                    picture = {"streams": [{"codec_name": "mjpeg" if second else "png", "width": 600, "height": 900}]}
                    if second and defect == "dimensions":
                        picture["streams"][0]["width"] = 299
                    if second and defect == "ratio":
                        picture["streams"][0]["width"] = 3000
                    if second and defect == "format":
                        picture["streams"][0]["codec_name"] = "png"
                    with patch("character_swap.poc_media.IMAGE_LIMIT", 1 if second and defect == "size" else 10_000_000):
                        return validate_media(video, image,
                            probe=lambda p: fixtures.video_metadata() if Path(p).suffix == ".mp4" else picture,
                            decoder=lambda v, i: (120, (1280, 720), (600, 900)))
                runner = test3.Test3Runner(self.root / str(defect), validator=validator)
                with self.subTest(defect=defect):
                    if defect is None:
                        self.plan(runner)
                    else:
                        with self.assertRaises(SafetyError):
                            self.plan(runner)
                        self.assertFalse(runner.manifest.exists())
