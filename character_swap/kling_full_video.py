"""Isolated five-segment CLI. No URLs or credentials are persisted.

Only submit/poll/download with --live can access the network. Spending is
estimated, not a provider billing guarantee. Never reset an uncertain ledger.
"""

import argparse
from copy import deepcopy
from dataclasses import replace
from fractions import Fraction
from functools import wraps
import json
import os
from pathlib import Path
import re
import tempfile
from threading import RLock

from .kling_http import KlingHTTPTransport, MediaDownloader, media_url
from .kling_poc import key_supplier, masked_input
from .kling_poc_test3 import EDIT_PROMPT
from .poc_media import IMAGE_LIMIT, VIDEO_LIMIT, decode_media, digest, probe_media, validate_media
from .providers.base import CharacterEditRequest, TaskStatus
from .providers.kling import KlingProvider
from .service import CharacterSwapService, SpendingPolicy
from .store import JobStore, SafetyError


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / 'uploads' / 'kling-full-video-20261107-171553'
WORKSPACE = BASE / 'kling-workflow'
REFERENCES = ROOT / 'uploads' / 'kling-omni-poc' / 'inputs'
REFERENCE_HASHES = (
    '7a3cde7547e6510c8c517c1e7dff0ce7998815fa323c7f3a2194e301b5afc48b',
    '96b8b3a9d68174bb011c23c337cd6c41f4212cc8af50de61de28752cf0da3a42',
)
SEGMENT_HASHES = (
    'e9a44063fa30d3712bef7f873a8539ad7dacf3618e323e1c68063785392e2f79',
    '63bd5b964a91e1fba2c9f754fe1cf5cfb7c3a46ffe00cb08b16d0265feb5e57d',
    'f84bd715d789ff25beb71de029b3e58d0cde51798186599d2d97dd904034c15d',
    'ba3c8e022d22b2717630a5d473cc89e88776b68a328753330dd2dd5f3c0c9af4',
    'ee1744fd299c7eb0c94a623a5d16502a5b0203e1be6cbd11c8ae138a9b762b15',
)
AUDIO_HASH = '539efd53a3431db67ec78c90de747b3c660932c5f2f2ea194ed40fd899f154f6'
FRAMES = (120, 120, 120, 120, 118)
ESTIMATE = 6000  # 1000 local units = one estimated API unit.
CAP = 30000
RESULT_MARKER = 'https://result.invalid/available'  # Not a provider URL.


def confirmation(segment):
    return f'AUTHORIZE SEGMENT {segment:02d} ESTIMATED 6 API UNITS'


def _serialized(operation):
    @wraps(operation)
    def wrapped(self, *args, **kwargs):
        # Avoid mixing ephemeral URLs between operations on one runner.
        # Cross-process spending/submission claims remain SQLite transactions.
        with self._operation_lock:
            return operation(self, *args, **kwargs)
    return wrapped


def validate_segment(video, image, frame_count, *, probe=probe_media, decoder=decode_media):
    """Reuse strict format/image checks, with independently checked timelines.

    Segment 05 has 118 frames and slightly shorter audio. Never stretch or
    silently pad inputs to satisfy the original five-second PoC validator.
    """
    expected = frame_count / 24

    def checked_probe(path):
        info = deepcopy(probe(path))
        if Path(path) == Path(video):
            try:
                streams = info['streams']
                v = next(s for s in streams if s['codec_type'] == 'video')
                a = next(s for s in streams if s['codec_type'] == 'audio')
                if (int(v['nb_read_frames']) != frame_count
                        or (int(v['width']), int(v['height'])) != (1920, 1080)):
                    raise ValueError
                for item in (v, info['format']):
                    if abs(Fraction(str(item['duration'])) - Fraction(frame_count, 24)) > Fraction(1, 1000):
                        raise ValueError
                # Preserve the approved short audio tail; no timing drift.
                if not Fraction(frame_count, 24) - Fraction(1, 20) <= Fraction(str(a['duration'])) <= Fraction(frame_count, 24) + Fraction(1, 1000):
                    raise ValueError
                for item in (v, a, info['format']):
                    if abs(Fraction(str(item['start_time']))) > Fraction(1, 1000):
                        raise ValueError
                    item['duration'] = '5'
                v['nb_read_frames'] = '120'
            except Exception:
                raise SafetyError('Segment timeline or resolution validation failed.') from None
        return info

    def checked_decode(v, i):
        count, size, image_size = decoder(v, i)
        if count != frame_count:
            raise SafetyError('Decoded segment frame count does not match approval.')
        return 120, size, image_size

    return validate_media(video, image, probe=checked_probe, decoder=checked_decode)


class _MemoryProvider(KlingProvider):
    """Resolve hash identifiers in memory; redact output URLs before storage."""

    def __init__(self, transport):
        super().__init__(transport)
        self.urls = None
        self.key = None
        self.output_url = None

    def submit(self, request):
        if self.urls is None or self.key is None:
            raise SafetyError('Runtime media URLs are required.')
        video, first, second = self.urls
        runtime = replace(request, video_uri=video, reference_image_uri=first,
                          second_reference_image_uri=second)
        payload = self.build_payload(runtime)
        payload['external_task_id'] = self.key
        return self._safe(self._parse_response(self.transport.post(self.TASK_PATH, json=payload)))

    def get_status(self, task_id):
        return self._safe(super().get_status(task_id))

    def _safe(self, result):
        if not re.fullmatch(r'[0-9]{1,64}', result.task_id):
            raise SafetyError('Invalid provider task identifier.')
        self.output_url = None
        if result.status == TaskStatus.SUCCEEDED:
            media_url(result.video_url)
            self.output_url = result.video_url
        return replace(result, video_url=RESULT_MARKER if self.output_url else None)


class FullVideoRunner:
    def __init__(self, workspace=WORKSPACE, *, base=BASE, references=REFERENCES,
                 enabled=False, transport=None, downloader=None, validator=validate_segment):
        self.workspace, self.base, self.references = map(Path, (workspace, base, references))
        # Prevent pointing the new runner at another experiment's ledger.
        if self.workspace.resolve() != (self.base / 'kling-workflow').resolve():
            raise SafetyError('Use the isolated full-video workflow directory.')
        if self.workspace.is_symlink():
            raise SafetyError('Workflow symlinks are not allowed.')
        self.workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        database = self.workspace / 'jobs.db'
        if database.is_symlink():
            raise SafetyError('Ledger symlinks are not allowed.')
        self.enabled = enabled
        self._operation_lock = RLock()
        self.validator = validator
        self.downloader = downloader if downloader is not None else MediaDownloader(enabled=enabled)
        self.provider = _MemoryProvider(transport)
        self.service = CharacterSwapService(
            JobStore(database), {'kling': self.provider}, {'kling': 'kling-v3-omni'},
            SpendingPolicy(mock_mode=enabled is not True, spending_enabled=enabled is True,
                           per_job_limit_units=ESTIMATE, total_limit_units=CAP))
        os.chmod(database, 0o600)

    @staticmethod
    def _index(segment):
        if type(segment) is not int or not 1 <= segment <= 5:
            raise SafetyError('Segment must be 01 through 05.')
        return segment - 1

    def _paths(self, segment):
        return (self.base / 'segments' / f'segment_{segment:02d}.mp4',
                self.references / 'test1.png', self.references / 'test2.jpeg')

    def _request(self, segment):
        i = self._index(segment)
        return CharacterEditRequest('sha256:' + SEGMENT_HASHES[i], 'sha256:' + REFERENCE_HASHES[0],
                                    EDIT_PROMPT, True, 'sha256:' + REFERENCE_HASHES[1], True)

    def _key(self, segment):
        self._index(segment)
        return f'kling-full-video-20261107-171553:segment:{segment:02d}:v1'

    def _validate(self, segment, paths=None):
        i = self._index(segment)
        video, first, second = paths if paths is not None else self._paths(segment)
        expected = (SEGMENT_HASHES[i], *REFERENCE_HASHES)
        if tuple(digest(p) for p in (video, first, second)) != expected:
            raise SafetyError('Media hashes do not match the approved inputs.')
        for image in (first, second):
            metadata = self.validator(video, image, FRAMES[i])
            if (metadata['video_sha256'], metadata['image_sha256']) != (expected[0], digest(image)):
                raise SafetyError('Media changed during validation.')
        if tuple(digest(p) for p in (video, first, second)) != expected:
            raise SafetyError('Media changed during validation.')

    def plan(self, segment):
        self._validate(segment)
        if digest(self.base / 'segments' / 'original_continuous_audio.m4a') != AUDIO_HASH:
            raise SafetyError('Original continuous audio does not match approval.')
        return self.service.create_job(self._key(segment), 'kling', self._request(segment), ESTIMATE)

    def job(self, segment):
        key = self._key(segment)
        with self.service.store._transaction() as connection:
            row = connection.execute('SELECT * FROM character_swap_jobs WHERE idempotency_key = ?', (key,)).fetchone()
            job = self.service.store._job(row)
        if (job.request != self._request(segment) or job.model != 'kling-v3-omni'
                or job.provider != 'kling' or job.estimated_units != ESTIMATE or job.prepared_request is not None):
            raise SafetyError('Stored job differs from the approved experiment.')
        return job

    def _live(self):
        if self.enabled is not True:
            raise SafetyError('Offline default: --live is required.')

    @_serialized
    def submit(self, segment, authorization, *, url_supplier):
        self._live()
        if authorization != confirmation(segment):
            raise SafetyError('Exact per-segment spending authorization is required.')
        job = self.job(segment)
        if job.status not in {'created', 'authorized'}:
            return job  # No URL prompts, downloads, or POST after a claim.
        self._validate(segment)
        urls = tuple(url_supplier())
        if len(urls) != 3:
            raise SafetyError('Three runtime media URLs are required.')
        for url in urls:
            media_url(url)
            # S3 origin restriction plus downloader's public DNS/TLS checks.
            host = media_url(url).hostname
            if (not host.startswith('ai-video-studio-poc-demo.s3.')
                    or not host.endswith('.amazonaws.com')):
                raise SafetyError('Use regional HTTPS URLs for the approved private S3 bucket.')
        with tempfile.TemporaryDirectory(dir=self.workspace) as temporary:
            paths = tuple(Path(temporary) / name for name in ('video.mp4', 'test1.png', 'test2.jpeg'))
            for url, path, limit in zip(urls, paths, (VIDEO_LIMIT, IMAGE_LIMIT, IMAGE_LIMIT)):
                self.downloader.download(url, path, max_bytes=limit)
            self._validate(segment, paths)
        self.provider.urls, self.provider.key = urls, job.idempotency_key
        try:
            self.service.authorize(job.id)
            return self.service.submit(job.id)
        finally:
            self.provider.urls = self.provider.key = self.provider.output_url = None

    @_serialized
    def poll(self, segment):
        self._live()
        job = self.job(segment)
        if not job.provider_task_id:
            raise SafetyError('No task ID; reconcile manually without resubmission.')
        try:
            return self.service.poll(job.id)
        finally:
            self.provider.output_url = None

    @_serialized
    def download(self, segment):
        self._live()
        job = self.job(segment)
        if job.status != 'succeeded' or not job.provider_task_id:
            raise SafetyError('Poll to confirmed success before downloading.')
        destination = self.workspace / f'segment_{segment:02d}_result.mp4'
        if destination.exists() or destination.is_symlink():
            raise SafetyError('Output already exists; it will not be overwritten.')
        try:
            result = self.provider.get_status(job.provider_task_id)
            if result.status != TaskStatus.SUCCEEDED or not self.provider.output_url:
                raise SafetyError('Provider output is not available.')
            return self.downloader.download(self.provider.output_url, destination, max_bytes=VIDEO_LIMIT)
        finally:
            self.provider.output_url = None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('plan', 'submit', 'poll', 'download'):
        command = commands.add_parser(name)
        command.add_argument('--segment', type=int, choices=range(1, 6), required=True)
        if name != 'plan':
            command.add_argument('--live', action='store_true')
        if name == 'submit':
            command.add_argument('--confirm-spend', required=True)
    args = parser.parse_args(argv)
    enabled = getattr(args, 'live', False)
    try:
        runner = FullVideoRunner(enabled=enabled,
            transport=KlingHTTPTransport(key_supplier(), enabled=enabled))
        if args.command == 'plan':
            job = runner.plan(args.segment)
        elif args.command == 'submit':
            def urls():
                return tuple(masked_input(label + ' HTTPS URL (hidden): ') for label in
                             ('Segment', 'Image 1 test1.png', 'Image 2 test2.jpeg'))
            job = runner.submit(args.segment, args.confirm_spend, url_supplier=urls)
        elif args.command == 'poll':
            job = runner.poll(args.segment)
        else:
            runner.download(args.segment)
            print('Output saved in the isolated workflow directory.')
            return 0
        print(json.dumps({'segment': args.segment, 'job_id': job.id, 'status': job.status,
                          'estimated_api_units': 6, 'aggregate_estimated_cap_api_units': 30,
                          'reserved_local_units': job.reserved_units, 'error_code': job.error_code}))
        return 0
    except Exception as error:
        # Never reflect arbitrary paths, URLs, transport bodies, or credentials.
        if (isinstance(error, SafetyError) and str(error) ==
                'Reliable media inspection unavailable: ffprobe is required; submission is blocked.'):
            print('Local media validation blocked: ffprobe was not found on this process PATH. '
                  'Add the verified portable FFmpeg bin directory to $env:PATH in this PowerShell terminal, '
                  'then rerun PLAN. No submission was attempted.')
            return 2
        print('Workflow operation blocked or failed. Review local job status; do not retry a submission blindly.')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
