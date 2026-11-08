"""Offline-capable adapter for Kling's legacy 3.0 Omni contract.

Contract: https://kling.ai/document-api/api/video/o1/video-omni/legacy
The injected transport returns decoded JSON objects. No HTTP implementation,
authentication, uploads, retries, or Flask integration is provided here.
"""

from typing import Protocol
from urllib.parse import quote, urlsplit

from .base import (
    CharacterEditRequest,
    CharacterSwapProvider,
    ProviderError,
    ProviderOperationError,
    TaskResult,
    TaskStatus,
    normalize_task_status,
)


class KlingTransport(Protocol):
    """Transport boundary; future transports use the official Kling API host."""

    def post(self, path: str, *, json: dict) -> dict:
        ...

    def get(self, path: str) -> dict:
        ...


class KlingProvider(CharacterSwapProvider):
    """Request base-video editing; scene preservation is not guaranteed.

    Spend authorization and MOCK_MODE checks belong to the calling service.
    Construction and payload building never invoke the transport.
    """

    TASK_PATH = "/v1/videos/omni-video"
    API_BASE_URL = "https://api-singapore.klingai.com"

    def __init__(self, transport: KlingTransport):
        self.transport = transport

    @staticmethod
    def _validate_url(value: str) -> None:
        try:
            parsed = urlsplit(value)
            valid = (
                parsed.scheme in {"http", "https"}
                and parsed.hostname
                and not parsed.username
                and not parsed.password
            )
        except (TypeError, ValueError, AttributeError):
            valid = False
        if not valid:
            raise ProviderOperationError(ProviderError(
                "invalid_input", "Media must use accessible HTTP(S) URLs."
            ))

    def build_payload(self, request: CharacterEditRequest) -> dict:
        """Use image_1 as a character reference, never as a first/last frame.

        Media reachability, adult identity, and file constraints cannot be
        verified here. The service must validate these before authorization.
        """
        self._validate_url(request.video_uri)
        self._validate_url(request.reference_image_uri)
        if request.second_reference_image_uri is not None:
            self._validate_url(request.second_reference_image_uri)
        if (not isinstance(request.prompt, str) or not isinstance(request.preserve_audio, bool)
                or not isinstance(request.complete_prompt, bool) or not request.prompt.strip() and request.complete_prompt):
            raise ProviderOperationError(ProviderError(
                "invalid_input", "Prompt and audio preference are invalid."
            ))
        prompt = (
            "Edit <<<video_1>>>. Replace only the main performer with the adult "
            "in <<<image_1>>>, including their face, body, hairstyle, and clothing. "
            "Preserve the original choreography, body movements, camera angles "
            "and camera movement, background and environment, lighting, "
            "supporting characters, other objects, and timing. "
        )
        if request.preserve_audio:
            prompt += "Preserve the original audio. "
        if request.prompt.strip():
            prompt += "Additional editing instructions: " + request.prompt.strip()
        prompt = prompt.strip()
        if request.complete_prompt:
            prompt = request.prompt
        if len(prompt) > 2500:
            raise ProviderOperationError(ProviderError(
                "invalid_input", "The complete editing prompt must not exceed 2500 characters."
            ))
        return {
            "model_name": "kling-v3-omni",
            "prompt": prompt,
            "image_list": [{"image_url": uri} for uri in (
                [request.reference_image_uri] + ([request.second_reference_image_uri]
                    if request.second_reference_image_uri is not None else []))],
            "video_list": [{
                "video_url": request.video_uri,
                "refer_type": "base",
                "keep_original_sound": "yes" if request.preserve_audio else "no",
            }],
            "multi_shot": False,
            "sound": "off",
            "mode": "pro",
        }

    def submit(self, request: CharacterEditRequest) -> TaskResult:
        payload = self.build_payload(request)
        try:
            response = self.transport.post(self.TASK_PATH, json=payload)
        except Exception:
            # A transport failure may occur after acceptance: never auto-retry.
            raise ProviderOperationError(ProviderError(
                "submission_unknown",
                "Submission outcome is unknown. Reconcile before submitting again.",
            )) from None
        return self._parse_response(response)

    def get_status(self, task_id: str) -> TaskResult:
        if not isinstance(task_id, str) or not task_id.strip():
            raise ProviderOperationError(ProviderError(
                "invalid_input", "A nonempty task ID is required."
            ))
        try:
            response = self.transport.get(self.TASK_PATH + "/" + quote(task_id, safe=""))
        except Exception:
            raise ProviderOperationError(ProviderError(
                "status_unavailable", "Unable to retrieve task status.", retryable=True,
            )) from None
        result = self._parse_response(response)
        if result.task_id != task_id:
            self._invalid_response()
        return result

    @staticmethod
    def _invalid_response():
        raise ProviderOperationError(ProviderError(
            "invalid_response",
            "Kling returned an invalid task response. Reconcile before resubmitting.",
        ))

    def _parse_response(self, response: dict) -> TaskResult:
        if not isinstance(response, dict) or type(response.get("code")) is not int:
            self._invalid_response()
        if response["code"] != 0:
            # Do not expose arbitrary provider messages or transport exceptions.
            raise ProviderOperationError(ProviderError(
                "kling_" + str(response["code"]), "Kling reported an API error."
            ))
        data = response.get("data")
        if not isinstance(data, dict):
            self._invalid_response()
        task_id = data.get("task_id")
        raw_status = data.get("task_status")
        if not isinstance(task_id, str) or not task_id.strip() or not isinstance(raw_status, str):
            self._invalid_response()
        # Restrict this adapter to documented legacy spellings.
        status = (
            normalize_task_status(raw_status)
            if raw_status in {"submitted", "processing", "succeed", "failed"}
            else TaskStatus.UNKNOWN
        )
        video_url = None
        error = None
        if status == TaskStatus.SUCCEEDED:
            task_result = data.get("task_result")
            videos = task_result.get("videos") if isinstance(task_result, dict) else None
            if not isinstance(videos, list) or not videos or not isinstance(videos[0], dict):
                self._invalid_response()
            video_url = videos[0].get("url")
            self._validate_url(video_url)
        elif status == TaskStatus.FAILED:
            error = ProviderError("task_failed", "Kling could not complete the character edit.")
        return TaskResult(task_id, status, video_url, error, raw_status)
