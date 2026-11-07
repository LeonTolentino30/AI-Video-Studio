"""Offline safety tests with temporary SQLite files and injected fake providers."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
import unittest

from character_swap.providers.base import (
    CharacterEditRequest, CharacterSwapProvider, ProviderError,
    ProviderOperationError, TaskResult, TaskStatus,
)
from character_swap.service import CharacterSwapService, SpendingPolicy
from character_swap.store import JobStore, SafetyError


class FakeProvider(CharacterSwapProvider):
    def __init__(self):
        self.submissions = []
        self.polls = []
        self.error = None
        self.result = TaskResult("provider-task", TaskStatus.PENDING)
        self.lock = threading.Lock()

    def submit(self, request):
        with self.lock:
            self.submissions.append(request)
        if self.error:
            raise self.error
        return self.result

    def get_status(self, task_id):
        self.polls.append(task_id)
        if self.error:
            raise self.error
        return self.result


class CharacterSwapSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "character-swap.db"
        self.store = JobStore(self.path)
        self.provider = FakeProvider()
        self.policy = SpendingPolicy(False, True, 100, 150)
        self.preparations = []
        self.request = CharacterEditRequest("original.mp4", "adult.png", "Replace the main performer.")
        self.service = self.make_service()

    def make_service(self, policy=None, preparer=None, store=None):
        return CharacterSwapService(store or self.store, {"fake": self.provider},
                                    {"fake": "offline-model"}, policy or self.policy, preparer)

    def job(self, key="key", estimate=60, service=None):
        return (service or self.service).create_job(key, "fake", self.request, estimate)

    def prepare_media(self, job):
        self.preparations.append(job.id)
        return replace(job.request, video_uri="prepared-video", reference_image_uri="prepared-photo")

    def test_default_policy_disables_all_provider_operations(self):
        service = self.make_service(SpendingPolicy(), self.prepare_media)
        job = self.job(service=service)
        for action in (service.authorize, service.prepare, service.submit, service.poll):
            with self.assertRaises(SafetyError):
                action(job.id)
        self.assertEqual(self.preparations, [])
        self.assertEqual(self.provider.submissions, [])
        self.assertEqual(self.provider.polls, [])

    def test_mock_mode_blocks_even_previously_authorized_jobs(self):
        job = self.job()
        self.service.authorize(job.id)
        service = self.make_service(replace(self.policy, mock_mode=True), self.prepare_media)
        for action in (service.prepare, service.submit, service.poll):
            with self.assertRaises(SafetyError):
                action(job.id)
        self.assertEqual(self.preparations + self.provider.submissions + self.provider.polls, [])

    def test_spending_requires_enabled_switch_and_positive_limits(self):
        job = self.job()
        for policy in (replace(self.policy, spending_enabled=False),
                       replace(self.policy, per_job_limit_units=0),
                       replace(self.policy, total_limit_units=0)):
            with self.assertRaises(SafetyError):
                self.make_service(policy).authorize(job.id)

    def test_explicit_authorization_required_before_prepare_or_submit(self):
        service = self.make_service(preparer=self.prepare_media)
        job = self.job(service=service)
        for action in (service.prepare, service.submit):
            with self.assertRaises(SafetyError):
                action(job.id)
        self.assertEqual(self.preparations + self.provider.submissions, [])

    def test_idempotency_key_reuses_job_but_rejects_changed_inputs(self):
        job = self.job()
        self.assertEqual(self.job().id, job.id)
        with self.assertRaises(SafetyError):
            self.job(estimate=61)
        with self.assertRaises(SafetyError):
            self.service.create_job("key", "fake", replace(self.request, prompt="Different"), 60)

    def test_per_job_and_total_limits_are_enforced(self):
        large = self.job("large", 101)
        with self.assertRaises(SafetyError):
            self.service.authorize(large.id)
        self.service.authorize(self.job("first", 100).id)
        second = self.job("second", 60)
        with self.assertRaises(SafetyError):
            self.service.authorize(second.id)
        self.assertEqual(self.store.get(second.id).reserved_units, 0)

    def test_reauthorization_does_not_reserve_twice(self):
        job = self.job(estimate=100)
        self.service.authorize(job.id)
        self.service.authorize(job.id)
        self.assertEqual(self.store.get(job.id).reserved_units, 100)
        self.service.authorize(self.job("remaining", 50).id)

    def test_concurrent_authorization_cannot_exceed_total_budget(self):
        jobs = [self.job(str(index), 100) for index in range(2)]
        def authorize(job):
            try:
                self.service.authorize(job.id)
                return True
            except SafetyError:
                return False
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(authorize, jobs))
        self.assertEqual(sum(results), 1)

    def test_concurrent_submission_invokes_provider_only_once(self):
        job = self.job()
        self.service.authorize(job.id)
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(self.service.submit, [job.id] * 4))
        self.assertTrue(all(result.id == job.id for result in results))
        self.assertEqual(len(self.provider.submissions), 1)
        self.assertEqual(self.store.get(job.id).reserved_units, 60)

    def test_concurrent_creation_reuses_one_idempotent_job(self):
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: self.job(), range(4)))
        self.assertEqual(len({job.id for job in results}), 1)

    def test_invalid_estimates_cannot_be_authorized(self):
        for estimate in (0, -1, True, 1.5, 2**63):
            with self.subTest(estimate=estimate):
                with self.assertRaises(SafetyError):
                    self.job(estimate=estimate)

    def test_prepare_is_authorized_and_cannot_be_bypassed_or_repeated(self):
        service = self.make_service(preparer=self.prepare_media)
        job = self.job(service=service)
        service.authorize(job.id)
        self.assertEqual(service.submit(job.id).status, "authorized")
        self.assertEqual(self.provider.submissions, [])
        service.prepare(job.id)
        service.prepare(job.id)
        service.submit(job.id)
        self.assertEqual(self.preparations, [job.id])
        self.assertEqual(self.provider.submissions[0].video_uri, "prepared-video")
        self.assertEqual(self.provider.submissions[0].prompt, self.request.prompt)

    def test_preparation_failure_keeps_reservation_and_is_not_retried(self):
        calls = []
        def fail(job):
            calls.append(job.id)
            raise TimeoutError("private diagnostics")
        service = self.make_service(preparer=fail)
        job = self.job(service=service)
        service.authorize(job.id)
        result = service.prepare(job.id)
        service.prepare(job.id)
        service.submit(job.id)
        self.assertEqual(result.status, "preparation_unknown")
        self.assertEqual(result.reserved_units, 60)
        self.assertNotIn("private", result.error_message)
        self.assertEqual(calls, [job.id])
        self.assertEqual(self.provider.submissions, [])

    def test_preparation_cannot_change_authorized_prompt(self):
        service = self.make_service(preparer=lambda job: replace(job.request, prompt="Changed edit"))
        job = self.job(service=service)
        service.authorize(job.id)
        self.assertEqual(service.prepare(job.id).status, "preparation_unknown")
        service.submit(job.id)
        self.assertEqual(self.provider.submissions, [])

    def test_ambiguous_submission_keeps_reservation_without_retry(self):
        job = self.job(estimate=100)
        self.service.authorize(job.id)
        self.provider.error = ProviderOperationError(ProviderError("timeout", "private", True))
        result = self.service.submit(job.id)
        self.service.submit(job.id)
        self.assertEqual(result.status, "submission_unknown")
        self.assertEqual(result.reserved_units, 100)
        self.assertEqual(len(self.provider.submissions), 1)
        self.assertNotIn("private", result.error_message)
        with self.assertRaises(SafetyError):
            self.service.authorize(self.job("next", 60).id)

    def test_restart_resumes_and_polls_by_local_job_id(self):
        job = self.job()
        self.service.authorize(job.id)
        self.service.submit(job.id)
        restarted = self.make_service(store=JobStore(self.path))
        self.assertEqual(restarted.get_job(job.id).provider_task_id, "provider-task")
        self.provider.result = TaskResult("provider-task", TaskStatus.SUCCEEDED,
                                          video_url="https://example.com/result.mp4")
        result = restarted.poll(job.id)
        self.assertEqual(result.video_url, "https://example.com/result.mp4")
        self.assertEqual(result.status, "succeeded")
        restarted.poll(job.id)
        self.assertEqual(self.provider.polls, ["provider-task"])
        self.assertEqual(len(self.provider.submissions), 1)

    def _complete_with_output(self, video_url, *, via_poll, key):
        job = self.job(key)
        self.service.authorize(job.id)
        if via_poll:
            self.provider.result = TaskResult("provider-task", TaskStatus.PENDING)
            self.service.submit(job.id)
        self.provider.result = TaskResult("provider-task", TaskStatus.SUCCEEDED, video_url=video_url)
        return self.service.poll(job.id) if via_poll else self.service.submit(job.id)

    def test_success_with_valid_output_url_is_persisted(self):
        url = "https://example.com/result.mp4?signature=placeholder&expires=123"
        for via_poll in (False, True):
            with self.subTest(via_poll=via_poll):
                result = self._complete_with_output(url, via_poll=via_poll, key=str(via_poll))
                saved = JobStore(self.path).get(result.id)
                self.assertEqual(saved.status, "succeeded")
                self.assertEqual(saved.video_url, url)
                self.assertIsNone(saved.error_code)
                self.assertEqual(saved.reserved_units, 60)

    def test_success_with_missing_output_url_is_failed(self):
        for via_poll in (False, True):
            with self.subTest(via_poll=via_poll):
                result = self._complete_with_output(None, via_poll=via_poll, key=str(via_poll))
                saved = JobStore(self.path).get(result.id)
                self.assertEqual(saved.status, "failed")
                self.assertEqual(saved.error_code, "invalid_output_url")
                self.assertIn("Manual reconciliation", saved.error_message)
                self.assertEqual(saved.provider_task_id, "provider-task")
                self.assertEqual(saved.reserved_units, 60)
                self.assertIsNone(saved.video_url)

    def test_success_with_empty_or_whitespace_url_is_failed(self):
        for index, url in enumerate(("", " ", "\t\r\n")):
            for via_poll in (False, True):
                with self.subTest(url=url, via_poll=via_poll):
                    # Each case uses a separate temporary budget ledger.
                    service = self.make_service(store=JobStore(Path(self.temp.name) / f"empty-{index}-{via_poll}.db"))
                    original = self.service
                    self.service = service
                    try:
                        result = self._complete_with_output(url, via_poll=via_poll, key="empty")
                    finally:
                        self.service = original
                    self.assertEqual(result.status, "failed")
                    self.assertEqual(result.error_code, "invalid_output_url")
                    self.assertEqual(result.reserved_units, 60)
                    self.assertIsNone(result.video_url)

    def test_malformed_or_nonstring_success_url_is_failed(self):
        for index, url in enumerate((123, "result.mp4", "https://", "javascript:alert(1)",
                                     "https://example.com:bad/result.mp4", "https://example.com/a b.mp4")):
            with self.subTest(url=url):
                service = self.make_service(store=JobStore(Path(self.temp.name) / f"invalid-{index}.db"))
                job = self.job(service=service)
                service.authorize(job.id)
                self.provider.result = TaskResult("provider-task", TaskStatus.SUCCEEDED, video_url=url)
                result = service.submit(job.id)
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.error_code, "invalid_output_url")
                self.assertEqual(result.reserved_units, 60)

    def test_invalid_output_failure_cannot_resubmit_or_release_reservation(self):
        for via_poll in (False, True):
            with self.subTest(via_poll=via_poll):
                submissions = len(self.provider.submissions)
                result = self._complete_with_output(None, via_poll=via_poll, key=str(via_poll))
                polls = len(self.provider.polls)
                restarted = self.make_service(store=JobStore(self.path))
                restarted.authorize(result.id)
                restarted.submit(result.id)
                restarted.poll(result.id)
                self.assertEqual(len(self.provider.submissions), submissions + 1)
                self.assertEqual(len(self.provider.polls), polls)
                self.assertEqual(restarted.get_job(result.id).reserved_units, 60)
                self.assertEqual(restarted.get_job(result.id).status, "failed")
        with self.assertRaises(SafetyError):
            self.service.authorize(self.job("over-budget").id)

    def test_poll_failure_is_recoverable_without_resubmission(self):
        job = self.job()
        self.service.authorize(job.id)
        self.service.submit(job.id)
        self.provider.error = TimeoutError("private")
        result = self.service.poll(job.id)
        self.assertEqual(result.status, "pending")
        self.assertEqual(result.error_code, "status_unavailable")
        self.provider.error = None
        self.provider.result = TaskResult("provider-task", TaskStatus.RUNNING)
        self.assertIsNone(self.service.poll(job.id).error_code)
        self.assertEqual(len(self.provider.submissions), 1)

    def test_failed_job_does_not_assume_provider_refund(self):
        job = self.job(estimate=100)
        self.service.authorize(job.id)
        self.provider.result = TaskResult("provider-task", TaskStatus.FAILED,
                                          error=ProviderError("failed", "private"))
        result = self.service.submit(job.id)
        self.assertEqual(result.error_code, "task_failed")
        self.assertEqual(result.reserved_units, 100)
        self.service.submit(job.id)
        self.assertEqual(len(self.provider.submissions), 1)
        with self.assertRaises(SafetyError):
            self.service.authorize(self.job("next", 60).id)

    def test_policy_limits_are_rechecked_before_provider_call(self):
        job = self.job()
        self.service.authorize(job.id)
        restricted = self.make_service(replace(self.policy, per_job_limit_units=50))
        with self.assertRaises(SafetyError):
            restricted.submit(job.id)
        self.assertEqual(self.provider.submissions, [])

    def test_changed_model_configuration_cannot_submit_an_old_authorization(self):
        job = self.job()
        self.service.authorize(job.id)
        changed = CharacterSwapService(self.store, {"fake": self.provider},
                                       {"fake": "different-model"}, self.policy)
        with self.assertRaises(SafetyError):
            changed.submit(job.id)
        self.assertEqual(self.provider.submissions, [])

    def test_mismatched_poll_result_does_not_replace_saved_task(self):
        job = self.job()
        self.service.authorize(job.id)
        self.service.submit(job.id)
        self.provider.result = TaskResult("wrong-task", TaskStatus.SUCCEEDED, video_url="wrong.mp4")
        result = self.service.poll(job.id)
        self.assertEqual(result.provider_task_id, "provider-task")
        self.assertEqual(result.status, "pending")
        self.assertIsNone(result.video_url)
        self.assertEqual(result.error_code, "status_unavailable")

    def test_stale_poll_update_cannot_overwrite_terminal_result(self):
        job = self.job()
        newer = self.store.update(job, status="succeeded", video_url="result.mp4")
        result = self.store.update(job, status="running")
        self.assertEqual(result.status, newer.status)
        self.assertEqual(result.video_url, newer.video_url)

    def test_crashed_submission_claim_is_never_automatically_replayed(self):
        job = self.job()
        self.service.authorize(job.id)
        self.store.claim(job.id, ("authorized",), "submitting", 100, 150)
        restarted = self.make_service(store=JobStore(self.path))
        self.assertEqual(restarted.submit(job.id).status, "submitting")
        self.assertEqual(self.provider.submissions, [])

    def test_separate_tables_and_local_resume_without_provider_access(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("CREATE TABLE videos (prompt TEXT)")
            connection.execute("INSERT INTO videos VALUES ('existing text-to-video')")
            connection.commit()
        job = self.job()
        default = self.make_service(SpendingPolicy())
        self.assertEqual(default.get_job(job.id).model, "offline-model")
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT prompt FROM videos").fetchone()[0],
                             "existing text-to-video")
        self.assertEqual(self.provider.polls + self.provider.submissions, [])


if __name__ == "__main__":
    unittest.main()
