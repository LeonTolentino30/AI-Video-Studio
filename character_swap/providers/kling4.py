"""Kling 4.0 serialization and task parsing; live submission stays blocked.

Endpoint, contents array, prompt and input roles are confirmed by the user.
Asset url/id fields and 1080p/adaptive settings are also confirmed by the user.
Receipt/query schemas are confirmed. Audio/account authorization is unresolved.
"""

from dataclasses import dataclass

from .base import CharacterSwapProvider
from .base import CharacterEditRequest
from ..kling_http import media_url
from ..kling_poc_test3 import EDIT_PROMPT as TEST3_PROMPT
from ..store import SafetyError
from ..kling4_results import identifier, parse_receipt, parse_query


MODEL = 'kling-4.0'
SUBMISSION_PATH = '/omni-video/kling-4.0'
SUBMISSION_URL = 'https://api-singapore.klingai.com' + SUBMISSION_PATH
VIDEO_ID = 'original_video'
PRIMARY_ID = 'primary_person'
SECONDARY_ID = 'additional_identity'
EDIT_PROMPT = (TEST3_PROMPT.replace('<<<video_1>>>', '@' + VIDEO_ID)
               .replace('<<<image_1>>>', '@' + PRIMARY_ID)
               .replace('<<<image_2>>>', '@' + SECONDARY_ID))
BLOCKERS = (
    'Original-audio retention or documented absence of that capability',
    'Account access and compatible Flagship package/balance',
    'Explicit spending authorization for a live Kling 4.0 submission',
)


@dataclass(frozen=True)
class EditIntent:
    """Internal requirements only; never send as a JSON request body."""

    endpoint: str = SUBMISSION_PATH
    prompt: str = EDIT_PROMPT
    input_roles: tuple = (('base_video', VIDEO_ID), ('refer_image', PRIMARY_ID),
                          ('refer_image', SECONDARY_ID))
    requested_resolution: str = '1080p'
    requested_aspect_ratio: str = 'adaptive'
    preserve_original_audio: bool = True


class Kling4Provider(CharacterSwapProvider):
    """Fail closed until the complete official API contract is available.

    No HTTP transport or credentials are created here. A separately enabled
    GET-only transport can be injected; tests use fakes. Protected output URLs
    are kept in memory only and replaced with a non-downloadable marker in
    normalized TaskResult objects, so the shared service cannot persist them.
    """

    @staticmethod
    def build_payload(request):
        """Build only the confirmed request shape, in memory.

        Callers must bind runtime URLs to approved hashes before a future live
        submission. Serialization does not fetch assets or authorize spending.
        No signed URL is persisted, printed, or included in validation errors.
        """
        if (not isinstance(request, CharacterEditRequest) or request.prompt != EDIT_PROMPT
                or request.complete_prompt is not True or request.preserve_audio is not True
                or request.second_reference_image_uri is None):
            raise SafetyError('The exact reviewed Kling 4.0 edit and two references are required.')
        for uri in (request.video_uri, request.reference_image_uri, request.second_reference_image_uri):
            media_url(uri)
        return {
            'contents': [
                {'type': 'prompt', 'text': EDIT_PROMPT},
                {'type': 'base_video', 'url': request.video_uri, 'id': VIDEO_ID},
                {'type': 'refer_image', 'url': request.reference_image_uri, 'id': PRIMARY_ID},
                {'type': 'refer_image', 'url': request.second_reference_image_uri, 'id': SECONDARY_ID},
            ],
            'settings': {'resolution': '1080p', 'aspect_ratio': 'adaptive'},
        }

    def __init__(self, transport=None):
        self.transport = transport
        self.last_report = None
        self._download_url = None

    def parse_response(self, response):
        """Normalize a documented creation receipt; never sends a POST."""
        self.last_report = self._download_url = None
        self.last_report, self._download_url = parse_receipt(response)
        return self.last_report.result

    def take_protected_video_url(self):
        """Consume a URL for an immediate private download, never for storage."""
        value, self._download_url = self._download_url, None
        return value

    def submit(self, request):
        raise SafetyError('Kling 4.0 submission is disabled pending contract and billing verification.')

    def get_status(self, task_id):
        self.last_report = self._download_url = None
        task_id = identifier(task_id)
        if self.transport is None:
            raise SafetyError('Kling 4.0 polling transport is disabled.')
        try:
            response = self.transport.get('/tasks?task_ids=' + task_id)
            self.last_report, self._download_url = parse_query(response, task_id)
            return self.last_report.result
        except Exception:
            raise SafetyError('Kling 4.0 polling failed; no task state was accepted.') from None
