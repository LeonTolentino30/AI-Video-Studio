"""Offline legacy Kling contract tests using only an injected fake transport."""

import unittest

from character_swap.providers.base import (
    CharacterEditRequest, CharacterSwapProvider, ProviderOperationError, TaskStatus,
)
from character_swap.providers.kling import KlingProvider


def response(status="submitted", **fields):
    return {"code": 0, "data": {"task_id": "task-123", "task_status": status, **fields}}


class FakeTransport:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def post(self, path, *, json):
        self.calls.append(("POST", path, json))
        if self.error:
            raise self.error
        return self.result

    def get(self, path):
        self.calls.append(("GET", path))
        if self.error:
            raise self.error
        return self.result


class KlingProviderTests(unittest.TestCase):
    def setUp(self):
        self.transport = FakeTransport(response())
        self.provider = KlingProvider(self.transport)
        self.request = CharacterEditRequest(
            "https://example.com/original.mp4",
            "https://example.com/adult-reference.png",
            "The main performer is at the center of the frame.",
        )

    def test_payload_uses_only_documented_base_edit_fields(self):
        payload = self.provider.build_payload(self.request)
        self.assertEqual(set(payload), {
            "model_name", "prompt", "image_list", "video_list", "multi_shot", "sound", "mode"
        })
        self.assertEqual(payload["model_name"], "kling-v3-omni")
        self.assertEqual(payload["image_list"], [{"image_url": self.request.reference_image_uri}])
        self.assertEqual(payload["video_list"], [{
            "video_url": self.request.video_uri, "refer_type": "base", "keep_original_sound": "yes"
        }])
        self.assertFalse(payload["multi_shot"])
        self.assertEqual(payload["sound"], "off")
        self.assertEqual(payload["mode"], "pro")
        self.assertEqual(self.transport.calls, [])

    def test_prompt_targets_full_body_and_requests_preservation(self):
        prompt = self.provider.build_payload(self.request)["prompt"]
        for phrase in (
            "<<<video_1>>>", "<<<image_1>>>", "adult", "face", "body", "hairstyle",
            "clothing", "choreography", "body movements", "camera angles", "camera movement",
            "background", "lighting", "supporting characters", "other objects", "timing",
            "original audio", self.request.prompt,
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, prompt)

    def test_audio_opt_out_uses_documented_no_value(self):
        payload = self.provider.build_payload(CharacterEditRequest(
            self.request.video_uri, self.request.reference_image_uri, "", preserve_audio=False
        ))
        self.assertEqual(payload["video_list"][0]["keep_original_sound"], "no")
        self.assertEqual(payload["sound"], "off")
        self.assertNotIn("Preserve the original audio", payload["prompt"])

    def test_submission_returns_receipt_and_makes_one_transport_call(self):
        self.assertIsInstance(self.provider, CharacterSwapProvider)
        result = self.provider.submit(self.request)
        self.assertEqual(result.task_id, "task-123")
        self.assertEqual(result.status, TaskStatus.PENDING)
        self.assertEqual(self.transport.calls, [
            ("POST", "/v1/videos/omni-video", self.provider.build_payload(self.request))
        ])

    def test_polling_normalizes_all_documented_states(self):
        for raw, expected in {
            "submitted": TaskStatus.PENDING, "processing": TaskStatus.RUNNING,
            "succeed": TaskStatus.SUCCEEDED, "failed": TaskStatus.FAILED,
        }.items():
            with self.subTest(raw=raw):
                self.transport.result = response(raw, task_result={
                    "videos": [{"url": "https://example.com/result.mp4"}]
                })
                result = self.provider.get_status("task-123")
                self.assertEqual(result.status, expected)
                self.assertEqual(result.raw_status, raw)
                if raw == "succeed":
                    self.assertEqual(result.video_url, "https://example.com/result.mp4")
                else:
                    self.assertIsNone(result.video_url)
                if raw == "failed":
                    self.assertEqual(result.error.code, "task_failed")
                    self.assertFalse(result.error.retryable)
        self.assertTrue(all(call == ("GET", "/v1/videos/omni-video/task-123")
                            for call in self.transport.calls))

    def test_unknown_status_remains_nonterminal(self):
        self.transport.result = response("future-state")
        result = self.provider.get_status("task-123")
        self.assertEqual(result.status, TaskStatus.UNKNOWN)
        self.assertEqual(result.raw_status, "future-state")
        self.assertFalse(result.status.is_terminal)

    def test_invalid_inputs_do_not_invoke_transport(self):
        for video, photo, prompt in (
            ("runway://video", self.request.reference_image_uri, "Edit"),
            (self.request.video_uri, "local-photo.png", "Edit"),
            ("https://user:password@example.com/video", self.request.reference_image_uri, "Edit"),
            (self.request.video_uri, self.request.reference_image_uri, "x" * 2500),
        ):
            with self.subTest(video=video, prompt_length=len(prompt)):
                with self.assertRaises(ProviderOperationError):
                    self.provider.submit(CharacterEditRequest(video, photo, prompt))
        self.assertEqual(self.transport.calls, [])

    def test_invalid_task_id_does_not_invoke_transport(self):
        with self.assertRaises(ProviderOperationError):
            self.provider.get_status("")
        self.assertEqual(self.transport.calls, [])

    def test_task_id_is_encoded_as_one_path_segment(self):
        self.transport.result = {"code": 0, "data": {
            "task_id": "a/b?c", "task_status": "processing"
        }}
        self.provider.get_status("a/b?c")
        self.assertEqual(self.transport.calls, [("GET", "/v1/videos/omni-video/a%2Fb%3Fc")])

    def test_mismatched_task_id_is_rejected(self):
        with self.assertRaises(ProviderOperationError):
            self.provider.get_status("different-task")

    def test_malformed_response_is_rejected_without_retry(self):
        for malformed in (None, {}, {"code": False}, {"code": 0, "data": {}},
                          response("succeed"), response("succeed", task_result={"videos": []})):
            with self.subTest(response=malformed):
                self.transport.result = malformed
                with self.assertRaises(ProviderOperationError) as raised:
                    self.provider.submit(self.request)
                self.assertFalse(raised.exception.error.retryable)

    def test_api_error_does_not_expose_raw_message(self):
        self.transport.result = {"code": 123, "message": "sensitive provider diagnostics"}
        with self.assertRaises(ProviderOperationError) as raised:
            self.provider.submit(self.request)
        self.assertEqual(raised.exception.error.code, "kling_123")
        self.assertNotIn("sensitive", str(raised.exception))

    def test_submission_timeout_is_ambiguous_and_never_retried(self):
        self.transport.error = TimeoutError("private transport diagnostics")
        with self.assertRaises(ProviderOperationError) as raised:
            self.provider.submit(self.request)
        self.assertEqual(raised.exception.error.code, "submission_unknown")
        self.assertFalse(raised.exception.error.retryable)
        self.assertNotIn("private", str(raised.exception))
        self.assertEqual(len(self.transport.calls), 1)

    def test_status_transport_failure_is_retryable_without_submission(self):
        self.transport.error = TimeoutError("private transport diagnostics")
        with self.assertRaises(ProviderOperationError) as raised:
            self.provider.get_status("task-123")
        self.assertEqual(raised.exception.error.code, "status_unavailable")
        self.assertTrue(raised.exception.error.retryable)
        self.assertEqual(self.transport.calls, [("GET", "/v1/videos/omni-video/task-123")])


if __name__ == "__main__":
    unittest.main()
