"""Offline Character Swap orchestration; no Flask startup or provider networking.

This is a trusted server-side API, not an HTTP authorization mechanism. Future
routes must authenticate callers and never let clients supply policy, authorize
their own jobs, or choose trusted cost estimates. Provider/model configuration
and estimates are supplied by server code. Reservations bound local estimated
exposure only, not actual bills, other applications, or provider-side retries.
"""

from dataclasses import dataclass
from typing import Callable, Mapping
from urllib.parse import urlsplit

from .providers.base import CharacterEditRequest, CharacterSwapProvider, TaskResult, TaskStatus
from .store import Job, JobStore, SafetyError


@dataclass(frozen=True)
class SpendingPolicy:
    mock_mode: bool = True
    spending_enabled: bool = False
    per_job_limit_units: int = 0
    total_limit_units: int = 0


class CharacterSwapService:
    def __init__(self, store: JobStore, providers: Mapping[str, CharacterSwapProvider],
                 models: Mapping[str, str], policy: SpendingPolicy = SpendingPolicy(),
                 prepare_media: Callable[[Job], CharacterEditRequest] | None = None):
        self.store = store
        self.providers = dict(providers)
        self.models = dict(models)
        self.policy = policy
        self.prepare_media = prepare_media

    def _require_enabled(self):
        policy = self.policy
        if policy.mock_mode is not False:
            raise SafetyError("Character Swap provider operations are disabled in demo mode")
        if policy.spending_enabled is not True:
            raise SafetyError("Character Swap spending is disabled")
        if any(type(value) is not int or not 0 < value <= 2**63 - 1 for value in (
            policy.per_job_limit_units, policy.total_limit_units
        )):
            raise SafetyError("Positive per-job and total spending limits must be configured")

    def _provider(self, job: Job) -> CharacterSwapProvider:
        if job.provider not in self.providers or self.models.get(job.provider) != job.model:
            raise SafetyError("The job's provider/model configuration is unavailable")
        return self.providers[job.provider]

    def create_job(self, key: str, provider: str, request: CharacterEditRequest,
                   estimated_units: int) -> Job:
        if provider not in self.providers or provider not in self.models:
            raise SafetyError("Unknown Character Swap provider")
        return self.store.create(key, provider, self.models[provider], request, estimated_units)

    def get_job(self, job_id: str) -> Job:
        """Resume local metadata without calling a provider, even in demo mode."""
        return self.store.get(job_id)

    def authorize(self, job_id: str) -> Job:
        """Explicit trusted-server approval binds the immutable stored request."""
        self._require_enabled()
        self._provider(self.store.get(job_id))
        return self.store.authorize(job_id, self.policy.per_job_limit_units, self.policy.total_limit_units)

    def prepare(self, job_id: str) -> Job:
        """Guard injected media preparation, including any future external upload.

        Preparation is assumed covered by the trusted estimate/reservation.
        Without an injected preparer, the submitted request is already prepared.
        No new provider interface method is needed.
        """
        self._require_enabled()
        self._provider(self.store.get(job_id))
        job, claimed = self.store.claim(job_id, ("authorized",), "preparing",
                                        self.policy.per_job_limit_units, self.policy.total_limit_units)
        if not claimed:
            return job
        try:
            prepared = self.prepare_media(job) if self.prepare_media else job.request
            if not isinstance(prepared, CharacterEditRequest):
                raise ValueError("Invalid prepared request")
            # Preparation changes asset identifiers, never approved edit content.
            if (prepared.prompt, prepared.preserve_audio) != (job.request.prompt, job.request.preserve_audio):
                raise ValueError("Preparation changed edit instructions")
            encoded = self.store.encode_request(prepared)
        except Exception:
            return self.store.update(job, status="preparation_unknown", error_code="preparation_unknown",
                                     error_message="Preparation failed or its outcome is unknown. Review required.")
        return self.store.update(job, status="prepared", prepared_json=encoded)

    def submit(self, job_id: str) -> Job:
        self._require_enabled()
        provider = self._provider(self.store.get(job_id))
        # When preparation is configured, it cannot be bypassed.
        allowed = ("prepared",) if self.prepare_media else ("authorized", "prepared")
        job, claimed = self.store.claim(job_id, allowed, "submitting",
                                        self.policy.per_job_limit_units, self.policy.total_limit_units)
        if not claimed:
            return job
        try:
            result = provider.submit(job.prepared_request or job.request)
            self._validate_result(result)
        except Exception:
            # Any exception may follow provider acceptance. Keep the reservation
            # and the same job; retryable transport errors never authorize replay.
            return self.store.update(job, status="submission_unknown", error_code="submission_unknown",
                                     error_message="Submission outcome is unknown. Do not resubmit automatically.")
        return self._save_result(job, result)

    @staticmethod
    def _validate_result(result: TaskResult):
        if (not isinstance(result, TaskResult) or not isinstance(result.task_id, str)
                or not result.task_id.strip() or not isinstance(result.status, TaskStatus)):
            raise ValueError("Invalid provider task result")

    @staticmethod
    def _valid_output_url(value) -> bool:
        """Check URL syntax locally; never fetch or alter signed output URLs."""
        if not isinstance(value, str) or not value or any(char.isspace() or ord(char) < 32 for char in value):
            return False
        try:
            parsed = urlsplit(value)
            return bool(parsed.scheme in {"http", "https"} and parsed.hostname
                        and not parsed.username and not parsed.password
                        and (parsed.port is None or parsed.port > 0))
        except ValueError:
            return False

    def _save_result(self, job: Job, result: TaskResult) -> Job:
        if result.status == TaskStatus.SUCCEEDED and not self._valid_output_url(result.video_url):
            return self.store.update(
                job, status="failed", provider_task_id=result.task_id, video_url=None,
                error_code="invalid_output_url",
                error_message="The provider reported success without a valid output video URL. Manual reconciliation required.",
            )
        return self.store.update(
            job, status=result.status.value, provider_task_id=result.task_id,
            video_url=result.video_url,
            error_code="task_failed" if result.status == TaskStatus.FAILED else None,
            error_message="The provider could not complete the edit." if result.status == TaskStatus.FAILED else None,
        )

    def poll(self, job_id: str) -> Job:
        self._require_enabled()
        job = self.store.get(job_id)
        provider = self._provider(job)
        if not job.provider_task_id or job.status in {"succeeded", "failed", "cancelled"}:
            return job
        try:
            result = provider.get_status(job.provider_task_id)
            self._validate_result(result)
            if result.task_id != job.provider_task_id:
                raise ValueError("Mismatched provider task ID")
        except Exception:
            return self.store.update(job, error_code="status_unavailable",
                                     error_message="Unable to retrieve task status. Polling may be retried.")
        return self._save_result(job, result)
