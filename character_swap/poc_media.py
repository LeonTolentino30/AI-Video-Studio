"""Fail-closed local media validation for one five-second experiment.

ffprobe is optional at install time but required to pass validation. It must
already exist on PATH; this module never downloads or installs it. OpenCV,
already a project dependency, decodes the reference and all video frames.
"""

from decimal import Decimal
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

from .store import SafetyError


VIDEO_LIMIT = 200_000_000
IMAGE_LIMIT = 10_000_000


def digest(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def probe_media(path):
    executable = shutil.which("ffprobe")
    if executable is None:
        raise SafetyError("Reliable media inspection unavailable: ffprobe is required; submission is blocked.")
    try:
        result = subprocess.run([
            executable, "-v", "error", "-protocol_whitelist", "file", "-count_frames", "-show_streams", "-show_format",
            "-of", "json", "-i", str(Path(path).resolve()),
        ], capture_output=True, timeout=60, check=True)
        if result.stderr.strip():
            raise ValueError
        return json.loads(result.stdout)
    except Exception:
        raise SafetyError("Reliable media inspection failed; submission is blocked.") from None


def decode_media(video, image):
    """Decode full video and image, not filename extensions alone."""
    import cv2
    reference = cv2.imread(str(image))
    if reference is None:
        raise SafetyError("Reference image cannot be decoded.")
    capture = cv2.VideoCapture(str(video))
    count = 0
    dimensions = None
    try:
        if not capture.isOpened():
            raise SafetyError("Video cannot be decoded.")
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            count += 1
            shape = (frame.shape[1], frame.shape[0])
            if dimensions is not None and shape != dimensions:
                raise SafetyError("Changing video dimensions are unsupported for this experiment.")
            dimensions = shape
    finally:
        capture.release()
    return count, dimensions, (reference.shape[1], reference.shape[0])


def validate_media(video, image, *, probe=probe_media, decoder=decode_media):
    """PoC policy: 120 frames at 24fps, five-second video AND audio timelines.

    H.264/AAC and constant 24fps are deliberately conservative PoC choices,
    not claims that Kling requires these codecs or only accepts this fps.
    Adult identity, consent, and outfit suitability require human review.
    """
    video, image = Path(video), Path(image)
    try:
        if video.suffix.lower() != ".mp4" or image.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
            raise ValueError
        if not video.is_file() or not image.is_file():
            raise ValueError
        if not 0 < video.stat().st_size <= VIDEO_LIMIT or not 0 < image.stat().st_size <= IMAGE_LIMIT:
            raise ValueError
        with video.open("rb") as source:
            if source.read(12)[4:8] != b"ftyp":
                raise ValueError
        with image.open("rb") as source:
            signature = source.read(8)
        png = signature == b"\x89PNG\r\n\x1a\n"
        if (image.suffix.lower() == ".png" and not png
                or image.suffix.lower() != ".png" and not signature.startswith(b"\xff\xd8\xff")):
            raise ValueError
        before = (digest(video), digest(image))
        info, picture = probe(video), probe(image)
        streams = info["streams"]
        videos = [stream for stream in streams if stream["codec_type"] == "video"]
        audio = [stream for stream in streams if stream["codec_type"] == "audio"]
        if len(videos) != 1 or len(audio) != 1:
            raise ValueError
        v, a = videos[0], audio[0]
        if "mp4" not in info["format"]["format_name"].split(",") or v["codec_name"] != "h264" or a["codec_name"] != "aac":
            raise ValueError
        if any(Fraction(v[key]) != 24 for key in ("avg_frame_rate", "r_frame_rate")):
            raise ValueError
        if int(v["nb_read_frames"]) != 120 or int(a["nb_read_frames"]) <= 0:
            raise ValueError
        for metadata in (info["format"], v, a):
            if abs(Decimal(str(metadata["duration"])) - 5) > Decimal("0.001"):
                raise ValueError
            if abs(Decimal(str(metadata["start_time"]))) > Decimal("0.001"):
                raise ValueError
        width, height = int(v["width"]), int(v["height"])
        if not 720 <= width <= 2160 or not 720 <= height <= 2160 or not Fraction(2, 5) <= Fraction(width, height) <= Fraction(5, 2):
            raise ValueError
        images = picture["streams"]
        if len(images) != 1 or images[0]["codec_name"] != ("png" if png else "mjpeg"):
            raise ValueError
        iw, ih = int(images[0]["width"]), int(images[0]["height"])
        if min(iw, ih) < 300 or not Fraction(2, 5) <= Fraction(iw, ih) <= Fraction(5, 2):
            raise ValueError
        frames, decoded_size, image_size = decoder(video, image)
        if frames != 120 or decoded_size != (width, height) or image_size != (iw, ih):
            raise ValueError
        if before != (digest(video), digest(image)):
            raise ValueError
        return {"video_sha256": before[0], "image_sha256": before[1], "duration_seconds": 5}
    except SafetyError:
        raise
    except Exception:
        raise SafetyError("Media failed the five-second video/audio validation policy.") from None
