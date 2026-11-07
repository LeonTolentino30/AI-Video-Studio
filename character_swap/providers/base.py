"""Shared Character Swap types. This module performs no I/O or networking."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


class TaskStatus(str, Enum):
    """Provider-independent states of a submitted editing task."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"

    @property
    def is_terminal(self) -> bool:
        return self in {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }


def normalize_task_status(status: str) -> TaskStatus:
    """Map common provider spellings; unfamiliar states remain nonterminal.

    Adapters may add provider-specific mappings before calling this helper.
    This mapping is not a declaration that any provider supports every state.
    """
    aliases = {
        "submitted": TaskStatus.PENDING,
        "pending": TaskStatus.PENDING,
        "queued": TaskStatus.PENDING,
        "throttled": TaskStatus.PENDING,
        "processing": TaskStatus.RUNNING,
        "running": TaskStatus.RUNNING,
        "succeed": TaskStatus.SUCCEEDED,
        "succeeded": TaskStatus.SUCCEEDED,
        "failed": TaskStatus.FAILED,
        "canceled": TaskStatus.CANCELLED,
        "cancelled": TaskStatus.CANCELLED,
    }
    return aliases.get(status.strip().lower(), TaskStatus.UNKNOWN)


@dataclass(frozen=True)
class CharacterEditRequest:
    """Edit the original video using the replacement adult's image reference.

    URIs are prepared asset identifiers understood by the selected adapter,
    not necessarily public URLs. The video is the base to edit, not a motion
    reference. Audio retention is a request, not a guarantee of support.
    """

    video_uri: str
    reference_image_uri: str
    prompt: str
    preserve_audio: bool = True


@dataclass(frozen=True)
class ProviderError:
    """Safe error information; never include credentials or raw HTTP headers.

    Retryability does not authorize repeating a paid submission. The service
    must reconcile ambiguous submissions before considering a retry.
    """

    code: str
    message: str
    retryable: bool = False


@dataclass(frozen=True)
class TaskResult:
    """A submission receipt or status snapshot, using the provider's task ID.

    raw_status preserves unfamiliar status text for diagnostics. A successful
    state alone does not guarantee an available output URL; callers must check.
    """

    task_id: str
    status: TaskStatus
    video_url: str | None = None
    error: ProviderError | None = None
    raw_status: str | None = None


class ProviderOperationError(Exception):
    """An operation failed without a usable task snapshot, e.g. a timeout."""

    def __init__(self, error: ProviderError):
        self.error = error
        super().__init__(error.message)


class CharacterSwapProvider(ABC):
    """Contract for Runway, Kling, or offline adapters.

    Implementations translate provider payloads into these types and raise
    ProviderOperationError for operation failures. Uploads, spending controls,
    persistence, and retry policy belong to the surrounding service layer.
    """

    @abstractmethod
    def submit(self, request: CharacterEditRequest) -> TaskResult:
        """Submit one base-video edit and return its task receipt."""
        raise NotImplementedError

    @abstractmethod
    def get_status(self, task_id: str) -> TaskResult:
        """Retrieve the status of an existing task without submitting another."""
        raise NotImplementedError
