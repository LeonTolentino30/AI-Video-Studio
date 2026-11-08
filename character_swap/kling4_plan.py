"""Offline, isolated planning for ONE full-video Kling 4.0 edit.

No live commands exist until the remaining official API details are verified.
No credentials, dotenv, production database, uploads, or provider calls.
"""

import argparse
from dataclasses import asdict
from fractions import Fraction
import json
import os
from pathlib import Path

from .poc_media import digest, probe_media, decode_media
from .providers.base import CharacterEditRequest
from .providers.kling4 import BLOCKERS, EDIT_PROMPT, MODEL, EditIntent, Kling4Provider
from .service import CharacterSwapService, SpendingPolicy
from .store import JobStore, SafetyError


ROOT = Path(__file__).resolve().parents[1]
INPUTS = ROOT / 'uploads' / 'kling-omni-poc' / 'inputs'
WORKSPACE = ROOT / 'uploads' / 'kling4-full-video'
EXPERIMENT_ID = 'kling4-full-video-project5-v1'
# SHA-256 pinned to the verified input, not a newly chosen file.
VIDEO_HASH = 'acc3ab79a57aec328d75b1b7990bd103db0feb78d4e52ecd4dac54b64d4b9466'
REFERENCE_HASHES = (
    '7a3cde7547e6510c8c517c1e7dff0ce7998815fa323c7f3a2194e301b5afc48b',
    '96b8b3a9d68174bb011c23c337cd6c41f4212cc8af50de61de28752cf0da3a42',
)
PROVISIONAL_ESTIMATE = 45000


def validate_video_limits(*, size, width, height, duration, fps, suffix):
    """Limits supplied from official documentation by the user; no wire fields."""
    try:
        if (suffix.lower() not in {'.mp4', '.mov'} or not 0 < size <= 300_000_000
                or not 700 <= width <= 4553 or not 700 <= height <= 4553
                or width * height > 8_294_400 or not 3 <= Fraction(str(duration)) <= 30
                or not 24 <= Fraction(str(fps)) <= 60):
            raise ValueError
    except Exception:
        raise SafetyError('Video exceeds the confirmed Kling 4.0 input limits.') from None


def validate_inputs(inputs=INPUTS, *, probe=probe_media, decoder=decode_media):
    """Validate the exact approved video/photos, fully offline and read-only.

    Image checks are conservative local policy, not verified Kling 4.0 maxima.
    Audio is inspected and retained locally; API audio behavior remains unknown.
    """
    folder = Path(inputs)
    video = folder / 'Video_Project_5_24fps.mp4'
    images = (folder / 'test1.png', folder / 'test2.jpeg')
    paths = (video, *images)
    try:
        before = tuple(digest(p) for p in paths)
        if before != (VIDEO_HASH, *REFERENCE_HASHES):
            raise ValueError
        with video.open('rb') as f:
            if f.read(12)[4:8] != b'ftyp':
                raise ValueError
        info = probe(video)
        streams = info['streams']
        v = [s for s in streams if s['codec_type'] == 'video']
        a = [s for s in streams if s['codec_type'] == 'audio']
        if len(v) != 1 or len(a) != 1:
            raise ValueError
        v, a = v[0], a[0]
        validate_video_limits(size=video.stat().st_size, width=int(v['width']), height=int(v['height']),
                              duration=v['duration'], fps=v['avg_frame_rate'], suffix=video.suffix)
        if (v['codec_name'] != 'h264' or a['codec_name'] != 'aac'
                or (int(v['width']), int(v['height'])) != (1920, 1080)
                or Fraction(v['avg_frame_rate']) != 24 or Fraction(v['r_frame_rate']) != 24
                or int(v['nb_read_frames']) != 598 or int(a['nb_read_frames']) <= 0
                or int(a['sample_rate']) != 48000 or int(a['channels']) != 2):
            raise ValueError
        for entry in (v, info['format']):
            if abs(Fraction(str(entry['duration'])) - Fraction(598, 24)) > Fraction(1, 1000):
                raise ValueError
        if not Fraction(598, 24) - Fraction(1, 20) <= Fraction(str(a['duration'])) <= Fraction(598, 24):
            raise ValueError
        for entry in (v, a, info['format']):
            if abs(Fraction(str(entry['start_time']))) > Fraction(1, 1000):
                raise ValueError
        for image, codec in zip(images, ('png', 'mjpeg')):
            if not 0 < image.stat().st_size <= 10_000_000:
                raise ValueError
            with image.open('rb') as f:
                signature = f.read(8)
            if (codec == 'png' and signature != b'\x89PNG\r\n\x1a\n'
                    or codec == 'mjpeg' and not signature.startswith(b'\xff\xd8\xff')):
                raise ValueError
            picture = probe(image)['streams']
            if len(picture) != 1 or picture[0]['codec_name'] != codec:
                raise ValueError
            iw, ih = int(picture[0]['width']), int(picture[0]['height'])
            if min(iw, ih) < 300 or not Fraction(2, 5) <= Fraction(iw, ih) <= Fraction(5, 2):
                raise ValueError
            count, dimensions, image_size = decoder(video, image)
            if (count, dimensions, image_size) != (598, (1920, 1080), (iw, ih)):
                raise ValueError
        if tuple(digest(p) for p in paths) != before:
            raise ValueError
        return {'frames': 598, 'fps': 24, 'duration_seconds': 598/24,
                'video_sha256': before[0], 'reference_sha256': list(before[1:]),
                'audio_codec': 'aac', 'audio_duration_seconds': float(a['duration'])}
    except SafetyError:
        raise
    except Exception:
        raise SafetyError('Approved full-video media validation failed.') from None


class Kling4Planner:
    def __init__(self, workspace=WORKSPACE, *, inputs=INPUTS, validator=validate_inputs):
        self.workspace, self.inputs = Path(workspace), Path(inputs)
        if self.workspace.name != 'kling4-full-video' or self.workspace.is_symlink():
            raise SafetyError('Use a separate kling4-full-video workspace.')
        self.validator = validator

    def plan(self):
        metadata = self.validator(self.inputs)
        if (metadata['video_sha256'], tuple(metadata['reference_sha256'])) != (VIDEO_HASH, REFERENCE_HASHES):
            raise SafetyError('Validation metadata differs from the approved media.')
        # Validate before creating any database. Store hash identifiers only.
        self.workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        database = self.workspace / 'jobs.db'
        if database.is_symlink():
            raise SafetyError('Ledger symlinks are not allowed.')
        self.service = CharacterSwapService(JobStore(database), {'kling4': Kling4Provider()},
            {'kling4': MODEL}, SpendingPolicy(mock_mode=True, spending_enabled=False,
                per_job_limit_units=PROVISIONAL_ESTIMATE, total_limit_units=PROVISIONAL_ESTIMATE))
        os.chmod(database, 0o600)
        request = CharacterEditRequest('sha256:' + VIDEO_HASH, 'sha256:' + REFERENCE_HASHES[0],
            EDIT_PROMPT, True, 'sha256:' + REFERENCE_HASHES[1], True)
        job = self.service.create_job(EXPERIMENT_ID, 'kling4', request, PROVISIONAL_ESTIMATE)
        return job, metadata


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['plan'])
    parser.parse_args(argv)
    try:
        job, metadata = Kling4Planner().plan()
        print(json.dumps({'job_id': job.id, 'status': job.status, 'media': metadata,
            'internal_edit_intent_not_wire_payload': asdict(EditIntent()),
            'requested_resolution': EditIntent().requested_resolution,
            'requested_aspect_ratio': EditIntent().requested_aspect_ratio,
            'provisional_estimated_api_units': 45, 'live_submission_enabled': False,
            'unverified_requirements': BLOCKERS}))
        return 0
    except Exception:
        print('Offline Kling 4.0 planning blocked: verify approved media and portable ffprobe availability. No submission attempted.')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
