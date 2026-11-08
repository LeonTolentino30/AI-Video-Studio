"""One fixed, separately authorized Kling experiment. No Flask imports.

Run with python -m character_swap.kling_poc. Default operation is local status.
Never delete the experiment directory or change its ID to retry an uncertain
submission. Local estimate reservations are NOT provider billing guarantees.
"""

import argparse
import getpass
import json
import os
from pathlib import Path
import tempfile
import time
import warnings

from .kling_http import KlingHTTPTransport, MediaDownloader, media_url
from .poc_media import IMAGE_LIMIT, VIDEO_LIMIT, digest, validate_media
from .providers.base import CharacterEditRequest
from .providers.kling import KlingProvider
from .service import CharacterSwapService, SpendingPolicy
from .store import JobStore, SafetyError


EXPERIMENT_ID = "kling-omni-pro-5s-v1"
CONFIRMATION = "SUBMIT ONE KLING OMNI EDIT ESTIMATED 6 API UNITS"
ESTIMATED_UNITS = 6000  # 1000 local accounting units = 1 estimated API unit.
WORKSPACE = Path(__file__).resolve().parents[1] / "uploads" / "kling-omni-poc"


class _OfflineTransport:
    def post(self, *args, **kwargs):
        raise SafetyError("Offline transport cannot submit.")

    def get(self, *args, **kwargs):
        raise SafetyError("Offline transport cannot poll.")


class _ExperimentProvider(KlingProvider):
    def __init__(self, transport, *, experiment_id=EXPERIMENT_ID):
        super().__init__(transport)
        self.experiment_id = experiment_id

    def build_payload(self, request):
        payload = super().build_payload(request)
        # Documented correlation field, NOT a provider idempotency guarantee.
        payload["external_task_id"] = self.experiment_id
        return payload


def masked_input(prompt):
    # getpass normally falls back to echoed input. Reject that fallback.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except (EOFError, getpass.GetPassWarning):
            raise SafetyError("A terminal supporting masked input is required.") from None


def key_supplier():
    """Cache a key in process memory only; do not read environment or files."""
    key = None

    def supply():
        nonlocal key
        if key is None:
            key = masked_input("Kling API key (hidden): ")
        return key

    return supply


class POCRunner:
    """Dependencies are injectable for offline tests; CLI workspace is fixed."""

    experiment_id = EXPERIMENT_ID

    def __init__(self, workspace=WORKSPACE, *, enabled=False, transport=None,
                 downloader=None, validator=validate_media, sleep=time.sleep):
        self.workspace = Path(workspace)
        if self.workspace.is_symlink():
            raise SafetyError("Experiment workspace cannot be a symbolic link.")
        self.workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.enabled = enabled
        self.validator = validator
        self.downloader = downloader if downloader is not None else MediaDownloader()
        self.sleep = sleep
        self.manifest = self.workspace / "experiment.json"
        database = self.workspace / "jobs.db"
        if database.is_symlink() or self.manifest.is_symlink():
            raise SafetyError("Experiment files cannot be symbolic links.")
        provider = _ExperimentProvider(transport if transport is not None else _OfflineTransport(),
                                       experiment_id=self.experiment_id)
        self.service = CharacterSwapService(
            JobStore(database), {"kling": provider}, {"kling": "kling-v3-omni"},
            SpendingPolicy(mock_mode=enabled is not True, spending_enabled=enabled is True,
                           per_job_limit_units=ESTIMATED_UNITS, total_limit_units=ESTIMATED_UNITS),
        )
        os.chmod(database, 0o600)

    def plan(self, video, reference, video_url, image_url, prompt="The main performer is centered in the frame.",
             *, second_reference=None, second_image_url=None, complete_prompt=False):
        """Local inspection only; no provider, storage, or credential calls."""
        media_url(video_url)
        media_url(image_url)
        metadata = self.validator(video, reference)
        if (second_reference is None) != (second_image_url is None):
            raise SafetyError("Both the second reference file and URL are required.")
        second_metadata = None
        if second_reference is not None:
            media_url(second_image_url)
            second_metadata = self.validator(video, second_reference)
            if second_metadata["video_sha256"] != metadata["video_sha256"]:
                raise SafetyError("Video changed during reference validation.")
        request = CharacterEditRequest(video_url, image_url, prompt, preserve_audio=True,
                                       second_reference_image_uri=second_image_url,
                                       complete_prompt=complete_prompt)
        self.service.providers["kling"].build_payload(request)
        job = self._plan_job(request)
        manifest = {
            "experiment_id": self.experiment_id, "job_id": job.id,
            "video": str(Path(video).resolve()), "reference": str(Path(reference).resolve()),
            "media": metadata,
        }
        if second_reference is not None:
            manifest["second_reference"] = str(Path(second_reference).resolve())
            manifest["second_media"] = second_metadata
        encoded = json.dumps(manifest, sort_keys=True)
        try:
            with self.manifest.open("x", encoding="utf-8") as output:
                os.chmod(self.manifest, 0o600)
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
        except FileExistsError:
            if json.loads(self.manifest.read_text(encoding="utf-8")) != manifest:
                self._replace_manifest(manifest)
        return job

    def _plan_job(self, request):
        return self.service.create_job(self.experiment_id, "kling", request, ESTIMATED_UNITS)

    def _replace_manifest(self, manifest):
        raise SafetyError("The fixed experiment is already bound to different inputs.")

    def _valid_plan_key(self, key):
        return key == self.experiment_id

    def _record(self):
        try:
            record = json.loads(self.manifest.read_text(encoding="utf-8"))
            job = self.service.get_job(record["job_id"])
            if (record["experiment_id"] != self.experiment_id or not self._valid_plan_key(job.idempotency_key)
                    or job.provider != "kling" or job.model != "kling-v3-omni"
                    or job.estimated_units != ESTIMATED_UNITS):
                raise ValueError
            return record, job
        except Exception:
            raise SafetyError("A valid existing experiment record is required; do not recreate an uncertain experiment.") from None

    def status(self):
        return self._record()[1]

    def _require_live(self):
        if self.enabled is not True:
            raise SafetyError("Offline default: explicit --live is required for network operations.")

    def submit(self, confirmation=None, *, accept_estimated_billing=False):
        self._require_live()
        if confirmation != CONFIRMATION or accept_estimated_billing is not True:
            raise SafetyError("Exact spending confirmation and acceptance of estimated billing are required.")
        record, job = self._record()
        if job.status not in {"created", "authorized"}:
            return job  # Includes submitting/submission_unknown after a crash.
        if self.validator(record["video"], record["reference"]) != record["media"]:
            raise SafetyError("Reviewed local media changed; submission is blocked.")
        if job.request.second_reference_image_uri is not None:
            if ("second_reference" not in record or "second_media" not in record
                    or self.validator(record["video"], record["second_reference"]) != record["second_media"]):
                raise SafetyError("Reviewed second reference changed; submission is blocked.")
        # Bind the fetched bytes to the reviewed local files before any paid POST.
        with tempfile.TemporaryDirectory(dir=self.workspace) as temporary:
            remote_video = Path(temporary) / "video.mp4"
            remote_image = Path(temporary) / ("reference" + Path(record["reference"]).suffix.lower())
            self.downloader.download(job.request.video_uri, remote_video, max_bytes=VIDEO_LIMIT)
            self.downloader.download(job.request.reference_image_uri, remote_image, max_bytes=IMAGE_LIMIT)
            if (digest(remote_video) != record["media"]["video_sha256"]
                    or digest(remote_image) != record["media"]["image_sha256"]):
                raise SafetyError("Hosted media does not match reviewed media; submission is blocked.")
            if self.validator(remote_video, remote_image) != record["media"]:
                raise SafetyError("Hosted media validation failed; submission is blocked.")
            if job.request.second_reference_image_uri is not None:
                second_image = Path(temporary) / ("second" + Path(record["second_reference"]).suffix.lower())
                self.downloader.download(job.request.second_reference_image_uri, second_image, max_bytes=IMAGE_LIMIT)
                if (digest(second_image) != record["second_media"]["image_sha256"]
                        or self.validator(remote_video, second_image) != record["second_media"]):
                    raise SafetyError("Hosted second reference does not match reviewed media.")
        self.service.authorize(job.id)
        return self.service.submit(job.id)

    def poll(self, *, count=1):
        self._require_live()
        if type(count) is not int or not 1 <= count <= 60:
            raise SafetyError("Polling count must be between 1 and 60.")
        job = self.status()
        if not job.provider_task_id:
            raise SafetyError("No provider task ID is saved. Manual reconciliation is required; do not resubmit.")
        for index in range(count):
            job = self.service.poll(job.id)
            if job.status in {"succeeded", "failed", "cancelled"} or job.error_code:
                break
            if index + 1 < count:
                self.sleep(5)
        return job

    def download(self):
        self._require_live()
        job = self.status()
        if job.status != "succeeded" or not job.video_url:
            raise SafetyError("No successful output is available.")
        return self.downloader.download(job.video_url, self.workspace / "result.mp4", max_bytes=VIDEO_LIMIT)


def summary(job):
    return {"experiment_id": job.idempotency_key, "job_id": job.id, "status": job.status,
            "estimated_api_units": 6, "reserved_local_units": job.reserved_units,
            "error_code": job.error_code}


def main(argv=None, *, runner_class=None, default_prompt="The main performer is centered in the frame.", allow_new_plan=False):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command")
    plan = commands.add_parser("plan", help="Offline local media inspection and immutable job creation")
    plan.add_argument("--video", required=True)
    plan.add_argument("--reference", required=True)
    plan.add_argument("--reference-2")
    plan.add_argument("--prompt", default=default_prompt)
    if allow_new_plan:
        plan.add_argument("--new-plan", action="store_true", help="Replace an unsubmitted Test 3 plan; preserve history")
    commands.add_parser("status", help="Read local status only")
    for name in ("submit", "poll", "download"):
        command = commands.add_parser(name)
        command.add_argument("--live", action="store_true", help="Enable this command's network operation")
        if name == "submit":
            command.add_argument("--confirm-spend", help="Must exactly match: " + CONFIRMATION)
            command.add_argument("--accept-estimated-billing", action="store_true",
                                 help="Local six-unit estimate does not guarantee the provider's actual bill")
        if name == "poll":
            command.add_argument("--count", type=int, default=1)
            command.add_argument("--debug", action="store_true",
                                 help="Show sanitized HTTP/code/status/request-ID diagnostics only")
    args = parser.parse_args(argv)
    enabled = getattr(args, "live", False)
    # Construction does not read the key. Only an enabled API request asks for it.
    diagnostics = None
    if args.command == "poll" and args.debug:
        diagnostics = lambda fields: print("Kling poll diagnostics: " + json.dumps(fields))
    transport = KlingHTTPTransport(key_supplier(), enabled=enabled, poll_diagnostics=diagnostics)
    try:
        runner_type = runner_class if runner_class is not None else POCRunner
        runner = runner_type(enabled=enabled, transport=transport, downloader=MediaDownloader(enabled=enabled))
        command = args.command or "status"
        if command == "plan":
            video_url = masked_input("Original-video HTTPS URL (hidden): ")
            image_url = masked_input("Reference-image HTTPS URL (hidden): ")
            extra = {}
            if getattr(args, "new_plan", False):
                extra["new_plan"] = True
            if args.reference_2 is not None:
                extra.update(second_reference=args.reference_2,
                             second_image_url=masked_input("Second reference-image HTTPS URL (hidden): "))
            job = runner.plan(args.video, args.reference, video_url, image_url, args.prompt, **extra)
        elif command == "submit":
            job = runner.submit(args.confirm_spend, accept_estimated_billing=args.accept_estimated_billing)
        elif command == "poll":
            job = runner.poll(count=args.count)
        elif command == "download":
            runner.download()
            print("Output saved privately in the fixed experiment workspace.")
            return 0
        else:
            job = runner.status()
        print(json.dumps(summary(job)))
        return 0
    except SafetyError as error:
        print(str(error))
        return 2
    except Exception:
        print("Experiment operation failed. Inspect the local job record before any further action.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
