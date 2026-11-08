"""Offline Test 2 isolation, prompt-only comparison, and safety regressions."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import character_swap.kling_poc as test1
import character_swap.kling_poc_test2 as test2
from character_swap.poc_media import digest
from character_swap.store import SafetyError
import test_kling_poc as fixtures


class SecondExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.video, self.photo = self.root / "source_5s_24fps.mp4", self.root / "IMG_1779.png"
        self.video.write_bytes(b"offline-video")
        self.photo.write_bytes(b"offline-reference")
        self.video_url, self.photo_url = "https://example.com/video", "https://example.com/photo"
        self.transport = fixtures.FakeTransport()
        self.downloader = fixtures.FakeDownloader({self.video_url: self.video.read_bytes(),
                                                   self.photo_url: self.photo.read_bytes()})
        self.validator = lambda video, photo: {
            "duration_seconds": 5, "video_sha256": digest(video), "image_sha256": digest(photo),
        }
        for name, value in (("VIDEO_SHA256", digest(self.video)), ("REFERENCE_SHA256", digest(self.photo))):
            replacement = patch.object(test2, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)

    def runner(self, enabled=False):
        return test2.Test2Runner(self.root / "test2", enabled=enabled, transport=self.transport,
                                downloader=self.downloader, validator=self.validator)

    def plan(self, runner):
        return runner.plan(self.video, self.photo, self.video_url, self.photo_url)

    def test_only_editing_prompt_and_experiment_identifier_change_in_payload(self):
        first = test1.POCRunner(self.root / "test1", validator=self.validator)
        first_job = first.plan(self.video, self.photo, self.video_url, self.photo_url)
        second = self.runner()
        second_job = self.plan(second)
        first_payload = first.service.providers["kling"].build_payload(first_job.request)
        second_payload = second.service.providers["kling"].build_payload(second_job.request)
        differences = {key for key in first_payload if first_payload[key] != second_payload[key]}
        self.assertEqual(differences, {"prompt", "external_task_id"})
        self.assertEqual(second_payload["external_task_id"], test2.EXPERIMENT_ID)
        self.assertEqual(first_job.idempotency_key, test1.EXPERIMENT_ID)
        self.assertEqual(second_job.idempotency_key, test2.EXPERIMENT_ID)
        self.assertEqual(first.service.policy, second.service.policy)
        self.assertEqual(first_job.estimated_units, second_job.estimated_units)

    def test_prompt_preserves_mouth_features_and_natural_singing(self):
        for phrase in ("mouth width", "lip proportions", "lip-corner", "mustache", "jawline",
                       "facial identity", "singing timing", "choreography", "background",
                       "camera movement", "audio"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, test2.EDIT_PROMPT)
        job = self.plan(self.runner())
        self.assertEqual(job.request.prompt, test2.EDIT_PROMPT)
        self.assertTrue(job.request.preserve_audio)

    def test_test1_ledger_manifest_and_result_are_unchanged(self):
        first = test1.POCRunner(self.root / "test1", validator=self.validator)
        first_job = first.plan(self.video, self.photo, self.video_url, self.photo_url)
        first.service.store.update(first_job, status="succeeded", video_url="https://example.com/result")
        (first.workspace / "result.mp4").write_bytes(b"existing-result")
        before = {path.name: digest(path) for path in first.workspace.iterdir() if path.is_file()}
        self.plan(self.runner())
        self.assertEqual(before, {path.name: digest(path) for path in first.workspace.iterdir() if path.is_file()})
        self.assertEqual(first.status().status, "succeeded")

    def test_production_workspaces_are_distinct_and_cannot_be_reused(self):
        self.assertNotEqual(test2.WORKSPACE, test1.WORKSPACE)
        with self.assertRaises(SafetyError):
            test2.Test2Runner(test1.WORKSPACE)

    def test_media_or_prompt_changes_are_rejected_before_job_creation(self):
        runner = self.runner()
        self.video.write_bytes(b"different")
        with self.assertRaises(SafetyError):
            self.plan(runner)
        self.video.write_bytes(b"offline-video")
        self.photo.write_bytes(b"different-photo")
        with self.assertRaises(SafetyError):
            self.plan(runner)
        self.photo.write_bytes(b"offline-reference")
        with self.assertRaises(SafetyError):
            runner.plan(self.video, self.photo, self.video_url, self.photo_url, "Different prompt")
        self.assertFalse(runner.manifest.exists())

    def test_offline_default_cannot_submit_or_reserve_spending(self):
        runner = self.runner()
        job = self.plan(runner)
        with self.assertRaises(SafetyError):
            runner.submit(test1.CONFIRMATION, accept_estimated_billing=True)
        self.assertEqual(runner.status().reserved_units, 0)
        self.assertEqual(job.status, "created")
        self.assertEqual(self.transport.posts + self.transport.gets + self.downloader.calls, [])

    def test_idempotency_spending_confirmation_and_ambiguous_timeout_are_preserved(self):
        runner = self.runner()
        job = self.plan(runner)
        live_fake = self.runner(True)
        with self.assertRaises(SafetyError):
            live_fake.submit()
        self.transport.error = TimeoutError("offline timeout")
        result = live_fake.submit(test1.CONFIRMATION, accept_estimated_billing=True)
        self.assertEqual(result.status, "submission_unknown")
        self.assertEqual(result.reserved_units, 6000)
        self.runner(True).submit(test1.CONFIRMATION, accept_estimated_billing=True)
        self.assertEqual(len(self.transport.posts), 1)
        self.assertEqual(self.plan(self.runner()).id, job.id)

    def test_test2_cli_plan_selects_the_fixed_prompt_and_workspace(self):
        real_class = test2.Test2Runner
        with patch.object(test2, "Test2Runner", side_effect=lambda **kwargs: real_class(
                self.root / "test2", validator=self.validator, **kwargs)), \
             patch("character_swap.kling_poc.masked_input", side_effect=[self.video_url, self.photo_url]), \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(test2.main(["plan", "--video", str(self.video), "--reference", str(self.photo)]), 0)
        self.assertEqual(json.loads(output.getvalue())["experiment_id"], test2.EXPERIMENT_ID)
        self.assertEqual(self.runner().status().request.prompt, test2.EDIT_PROMPT)


if __name__ == "__main__":
    unittest.main()
