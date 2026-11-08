"""Local-only restoration of original music-video audio; no provider calls.

Video packets are copied. Audio is encoded to AAC from the original source.
Small tail padding/trimming requires explicit approval; larger mismatches are
rejected. No playback-speed correction is ever performed.
"""

import argparse
from array import array
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from .kling4_plan import INPUTS, WORKSPACE
from .poc_media import digest
from .store import SafetyError


SOURCE = INPUTS / 'Video Project 5.mp4'
SOURCE_HASH = 'c80ac7cbc611174d6e2c318e3ce7bc0a01cda2c41e89cb3bb307d10699c30bdb'
EXPECTED_DURATION = Fraction(598, 24)
MAX_TAIL_ADJUSTMENT = Fraction(1, 4)


def _run(args):
    try:
        result = subprocess.run(args, capture_output=True, check=True, timeout=180)
        if result.stderr.strip():
            raise ValueError
        return result.stdout
    except Exception:
        raise SafetyError('Local FFmpeg inspection or conversion failed; no final file published.') from None


def _tools(tools_bin):
    result = []
    for name in ('ffmpeg', 'ffprobe'):
        if tools_bin is None:
            path = shutil.which(name)
        else:
            path = Path(tools_bin) / (name + ('.exe' if os.name == 'nt' else ''))
        if path is None or not Path(path).is_file():
            raise SafetyError('Verified ffmpeg and ffprobe are required on PATH or via --tools-bin.')
        result.append(str(Path(path).resolve()))
    return result


def _probe(path, ffprobe):
    return json.loads(_run([ffprobe, '-v', 'error', '-protocol_whitelist', 'file', '-count_frames',
                            '-show_streams', '-show_format', '-of', 'json', '-i', str(path)]))


def _video(info):
    streams = [s for s in info['streams'] if s['codec_type'] == 'video']
    if len(streams) != 1:
        raise ValueError
    v = streams[0]
    count = int(v['nb_read_frames'])
    duration = Fraction(str(v['duration']))
    if (v['codec_name'] != 'h264' or (int(v['width']), int(v['height'])) != (1920, 1080)
            or Fraction(v['avg_frame_rate']) != 24 or Fraction(v['r_frame_rate']) != 24
            or count <= 0 or abs(duration - Fraction(count, 24)) > Fraction(1, 1000)
            or abs(Fraction(str(v['start_time']))) > Fraction(1, 48000)
            or abs(duration - EXPECTED_DURATION) > MAX_TAIL_ADJUSTMENT):
        raise ValueError
    return count, Fraction(count, 24)


def _audio(info):
    streams = [s for s in info['streams'] if s['codec_type'] == 'audio']
    if len(streams) != 1:
        raise ValueError
    a = streams[0]
    if (a['codec_name'] != 'aac' or int(a['sample_rate']) != 48000 or int(a['channels']) != 2
            or int(a['nb_read_frames']) <= 0
            or abs(Fraction(str(a['start_time']))) > Fraction(1, 48000)):
        raise ValueError
    duration = Fraction(str(a['duration']))
    if duration <= 0:
        raise ValueError
    return duration


def _frame_hashes(path, ffmpeg):
    text = _run([ffmpeg, '-nostdin', '-v', 'error', '-protocol_whitelist', 'file', '-i', str(path),
                 '-map', '0:v:0', '-an', '-fps_mode', 'passthrough', '-f', 'framehash', '-hash', 'sha256', '-']).decode('ascii')
    # Compare decoded pixels and frame timing (PTS/duration), not just counts.
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return [line for line in lines if line.startswith('#tb ') or not line.startswith('#')]


def _validate_frame_timeline(lines, count):
    headers = [line for line in lines if line.startswith('#tb ')]
    frames = [line for line in lines if not line.startswith('#')]
    if len(headers) != 1 or len(frames) != count:
        raise ValueError
    timebase = Fraction(headers[0].split(':', 1)[1].strip())
    for index, line in enumerate(frames):
        fields = [field.strip() for field in line.split(',')]
        if (len(fields) != 6 or int(fields[2])*timebase != Fraction(index, 24)
                or int(fields[3])*timebase != Fraction(1, 24)):
            raise ValueError


def _pcm(path, ffmpeg, audio_filter):
    raw = _run([ffmpeg, '-nostdin', '-v', 'error', '-protocol_whitelist', 'file', '-i', str(path),
                '-map', '0:a:0', '-af', audio_filter, '-c:a', 'pcm_s16le', '-f', 's16le', '-'])
    values = array('h')
    values.frombytes(raw)
    if os.sys.byteorder != 'little':
        values.byteswap()
    return values


def _audio_match(source, result):
    if len(source) != len(result) or not source:
        raise ValueError
    energy_a = sum(x*x for x in source)
    energy_b = sum(x*x for x in result)
    if energy_a == 0:
        if any(abs(x) > 2 for x in result):
            raise ValueError
        return 1.0
    if energy_b == 0:
        raise ValueError
    correlation = sum(a*b for a,b in zip(source,result)) / math.sqrt(energy_a*energy_b)
    # Lossy AAC is expected; reject silence, a shifted track or unrelated audio.
    if correlation < 0.99:
        raise ValueError
    return correlation


def restore_audio(generated, output, *, source=SOURCE, tools_bin=None, allow_tail_adjustment=False):
    generated, output, source = map(Path, (generated, output, source))
    try:
        if (output.suffix.lower() != '.mp4' or output.exists() or output.is_symlink()
                or generated.is_symlink() or source.is_symlink()
                or output.resolve() in {generated.resolve(), source.resolve()}):
            raise SafetyError('Use a new MP4 output filename; inputs and existing files cannot be overwritten.')
        ffmpeg, ffprobe = _tools(tools_bin)
        before_source, before_generated = digest(source), digest(generated)
        if before_source != SOURCE_HASH:
            raise SafetyError('Original Video Project 5 audio source does not match its approved hash.')
        generated_info, source_info = _probe(generated, ffprobe), _probe(source, ffprobe)
        frames, duration = _video(generated_info)
        audio_duration = _audio(source_info)
        adjustment = duration - audio_duration
        if abs(adjustment) > MAX_TAIL_ADJUSTMENT:
            raise SafetyError('Audio/video duration mismatch exceeds 0.25 seconds; manual review is required.')
        if abs(adjustment) > Fraction(1, 48000) and allow_tail_adjustment is not True:
            raise SafetyError('Audio duration differs from video. Review the tail and use --allow-tail-adjustment to approve up to 0.25 seconds of audio-only padding or trimming.')
        samples = frames * 2000  # Exact 48 kHz sample boundary at 24 fps.
        # Respect the original MP4 audio presentation end, excluding any AAC
        # decoder padding beyond its declared timeline before explicit padding.
        source_samples = int(audio_duration * 48000 + Fraction(1, 2))
        audio_filter = (f'atrim=end_sample={source_samples},apad=whole_len={samples},'
                        f'atrim=end_sample={samples},asetpts=PTS-STARTPTS')
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.audio-restore-', dir=output.parent) as temporary:
            candidate = Path(temporary) / 'verified.mp4'
            _run([ffmpeg, '-nostdin', '-v', 'error', '-n', '-protocol_whitelist', 'file', '-i', str(generated),
                  '-protocol_whitelist', 'file', '-i', str(source), '-map', '0:v:0', '-map', '1:a:0',
                  '-c:v', 'copy', '-af', audio_filter, '-c:a', 'aac', '-b:a', '192k', '-ar', '48000',
                  '-ac', '2', '-map_metadata', '-1', '-movflags', '+faststart', str(candidate)])
            final_info = _probe(candidate, ffprobe)
            if _video(final_info) != (frames, duration):
                raise ValueError
            if abs(_audio(final_info) - duration) > Fraction(1, 1000):
                raise ValueError
            if abs(Fraction(str(final_info['format']['duration'])) - duration) > Fraction(1, 1000):
                raise ValueError
            original_frames, restored_frames = _frame_hashes(generated, ffmpeg), _frame_hashes(candidate, ffmpeg)
            _validate_frame_timeline(original_frames, frames)
            _validate_frame_timeline(restored_frames, frames)
            if original_frames != restored_frames or sum(not line.startswith('#') for line in restored_frames) != frames:
                raise ValueError
            original_audio = _pcm(source, ffmpeg, audio_filter)
            restored_audio = _pcm(candidate, ffmpeg, f'atrim=end_sample={samples}')
            if len(original_audio) != samples*2:
                raise ValueError
            correlation = _audio_match(original_audio, restored_audio)
            _run([ffmpeg, '-nostdin', '-v', 'error', '-xerror', '-protocol_whitelist', 'file',
                  '-i', str(candidate), '-f', 'null', '-'])
            if (digest(source), digest(generated)) != (before_source, before_generated):
                raise ValueError
            final_hash = digest(candidate)
            # Same-volume atomic, exclusive publication: never replace an output
            # created concurrently. Failed validation leaves no final file.
            os.link(candidate, output)
        return {'output': str(output.resolve()), 'duration_seconds': float(duration), 'frames': frames,
                'fps': 24, 'resolution': [1920,1080], 'video_stream_copied': True,
                'decoded_frames_and_timing_identical': True, 'audio': 'AAC stereo 48000 Hz from original Video Project 5',
                'audio_start_seconds': 0, 'audio_tail_adjustment_seconds': float(adjustment),
                'audio_tail_action': 'pad_silence' if adjustment > 0 else 'trim_tail' if adjustment < 0 else 'none',
                'original_audio_correlation': correlation, 'inputs_unchanged': True, 'sha256': final_hash}
    except SafetyError:
        raise
    except Exception:
        raise SafetyError('Audio restoration validation failed; no final file published.') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--generated', required=True)
    parser.add_argument('--output', default=str(WORKSPACE/'final_original_audio.mp4'))
    parser.add_argument('--tools-bin')
    parser.add_argument('--allow-tail-adjustment', action='store_true')
    args = parser.parse_args(argv)
    try:
        print(json.dumps(restore_audio(args.generated, args.output, tools_bin=args.tools_bin,
                                       allow_tail_adjustment=args.allow_tail_adjustment)))
        return 0
    except SafetyError as error:
        print(str(error))  # Only static, locally defined messages escape.
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
