"""Offline contract tests; no Flask startup, credentials, or provider SDKs."""

import unittest

from character_swap.providers.base import (
    CharacterEditRequest,
    CharacterSwapProvider,
    ProviderError,
    ProviderOperationError,
    TaskResult,
    TaskStatus,
    normalize_task_status,
)


class OfflineProvider(CharacterSwapProvider):
    def __init__(self):
        self.requests = []

    def submit(self, request):
        self.requests.append(request)
        return TaskResult("offline-task", TaskStatus.PENDING, raw_status="submitted")

    def get_status(self, task_id):
        return TaskResult(task_id, TaskStatus.SUCCEEDED, video_url="offline-result.mp4")


class ProviderContractTests(unittest.TestCase):
    def test_interface_requires_an_implementation(self):
        with self.assertRaises(TypeError):
            CharacterSwapProvider()

    def test_partial_adapter_cannot_be_instantiated(self):
        class SubmitOnly(CharacterSwapProvider):
            def submit(self, request):
                return TaskResult("task", TaskStatus.PENDING)

        with self.assertRaises(TypeError):
            SubmitOnly()

    def test_submit_and_poll_keep_base_video_and_reference_separate(self):
        provider = OfflineProvider()
        request = CharacterEditRequest(
            "original.mp4", "adult-reference.png", "Replace the main performer."
        )
        receipt = provider.submit(request)
        result = provider.get_status(receipt.task_id)
        self.assertEqual(provider.requests, [request])
        self.assertEqual(request.video_uri, "original.mp4")
        self.assertEqual(request.reference_image_uri, "adult-reference.png")
        self.assertTrue(request.preserve_audio)
        self.assertEqual(receipt.status, TaskStatus.PENDING)
        self.assertEqual(result.task_id, receipt.task_id)
        self.assertEqual(result.status, TaskStatus.SUCCEEDED)
        self.assertEqual(result.video_url, "offline-result.mp4")

    def test_audio_retention_can_be_disabled(self):
        request = CharacterEditRequest("clip", "photo", "Edit", preserve_audio=False)
        self.assertFalse(request.preserve_audio)

    def test_failed_task_carries_structured_error(self):
        error = ProviderError("edit_failed", "The edit could not be completed.")
        result = TaskResult("task", TaskStatus.FAILED, error=error)
        self.assertEqual(result.error, error)
        self.assertIsNone(result.video_url)
        self.assertFalse(error.retryable)

    def test_operation_error_without_task_receipt(self):
        error = ProviderError("submission_unknown", "Submission timed out.")
        with self.assertRaises(ProviderOperationError) as raised:
            raise ProviderOperationError(error)
        self.assertEqual(raised.exception.error, error)
        self.assertEqual(str(raised.exception), error.message)


class StatusNormalizationTests(unittest.TestCase):
    def test_existing_runway_status_spellings(self):
        for raw, expected in {
            "PENDING": TaskStatus.PENDING,
            "THROTTLED": TaskStatus.PENDING,
            "RUNNING": TaskStatus.RUNNING,
            "SUCCEEDED": TaskStatus.SUCCEEDED,
            "FAILED": TaskStatus.FAILED,
        }.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_task_status(raw), expected)

    def test_documented_kling_legacy_status_spellings(self):
        for raw, expected in {
            "submitted": TaskStatus.PENDING,
            "processing": TaskStatus.RUNNING,
            "succeed": TaskStatus.SUCCEEDED,
            "failed": TaskStatus.FAILED,
        }.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_task_status(raw), expected)

    def test_whitespace_case_and_other_common_aliases(self):
        self.assertEqual(normalize_task_status("  SuCcEeDeD  "), TaskStatus.SUCCEEDED)
        self.assertEqual(normalize_task_status("queued"), TaskStatus.PENDING)
        for raw in ("cancelled", "canceled"):
            self.assertEqual(normalize_task_status(raw), TaskStatus.CANCELLED)

    def test_unknown_state_is_not_terminal_or_successful(self):
        for raw in ("", "future_provider_state", "success_maybe"):
            with self.subTest(raw=raw):
                status = normalize_task_status(raw)
                self.assertEqual(status, TaskStatus.UNKNOWN)
                self.assertFalse(status.is_terminal)
        result = TaskResult("task", TaskStatus.UNKNOWN, raw_status="future_provider_state")
        self.assertEqual(result.raw_status, "future_provider_state")

    def test_only_success_failure_and_cancellation_are_terminal(self):
        terminal = {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}
        for status in TaskStatus:
            with self.subTest(status=status):
                self.assertEqual(status.is_terminal, status in terminal)
                self.assertEqual(normalize_task_status(status.value), status)


if __name__ == "__main__":
    unittest.main()
