"""Isolated two-image Test 3 runner; offline unless explicitly authorized."""

from pathlib import Path
import json
import os
import re
import tempfile

from .kling_poc import POCRunner, WORKSPACE as TEST1_WORKSPACE, main as run_cli
from .kling_poc_test2 import WORKSPACE as TEST2_WORKSPACE, VIDEO_SHA256
from .poc_media import digest
from .store import SafetyError

EXPERIMENT_ID = "kling-omni-pro-5s-v3"
REFERENCE_FILENAMES = ("test1.png", "test2.jpeg")
WORKSPACE = TEST1_WORKSPACE.with_name("kling-omni-poc-test3")
EDIT_PROMPT = (
    "Edit <<<video_1>>>. Replace only the main performer with the adult in "
    "<<<image_1>>>, including their face, body, hairstyle, and clothing. "
    "<<<image_2>>> shows the same adult from another angle and expression; "
    "use it only as an additional facial identity reference. Keep the clothing "
    "and overall appearance from <<<image_1>>>. Preserve the original "
    "choreography, body movements, camera angles and camera movement, "
    "background and environment, lighting, supporting characters, other "
    "objects, and timing. Preserve the original audio. Additional editing "
    "instructions: The main performer is at the center of the frame."
)


class Test3Runner(POCRunner):
    experiment_id = EXPERIMENT_ID

    def __init__(self, workspace=WORKSPACE, **kwargs):
        if Path(workspace).resolve() in {TEST1_WORKSPACE.resolve(), TEST2_WORKSPACE.resolve()}:
            raise SafetyError("Test 3 cannot use a previous experiment workspace.")
        super().__init__(workspace, **kwargs)

    def plan(self, video, reference, video_url, image_url, prompt=EDIT_PROMPT,
             *, second_reference=None, second_image_url=None, new_plan=False):
        if prompt != EDIT_PROMPT:
            raise SafetyError("Test 3 requires the exact reviewed prompt.")
        if (second_reference is None or second_image_url is None
                or (Path(reference).name, Path(second_reference).name) != REFERENCE_FILENAMES):
            raise SafetyError("Test 3 requires test1.png then test2.jpeg and both URLs.")
        if digest(video) != VIDEO_SHA256:
            raise SafetyError("Test 3 requires the unchanged validated Test 1 video.")
        self._new_plan = new_plan is True
        try:
            return super().plan(video, reference, video_url, image_url, prompt,
                                second_reference=second_reference, second_image_url=second_image_url,
                                complete_prompt=True)
        finally:
            self._new_plan = False

    def _valid_plan_key(self, key):
        return key == EXPERIMENT_ID or re.fullmatch(re.escape(EXPERIMENT_ID) + r":plan:[0-9a-f]{32}", key) is not None

    def _plan_job(self, request):
        if not self.manifest.exists():
            if self._new_plan:
                raise SafetyError("An existing plan manifest is required for --new-plan; do not recreate it.")
            return super()._plan_job(request)
        _, previous = self._record()
        if self._new_plan:
            return self.service.store.replace_unsubmitted_plan(previous.id, request)
        return self.service.create_job(previous.idempotency_key, "kling", request, previous.estimated_units)

    def _replace_manifest(self, manifest):
        if not self._new_plan:
            return super()._replace_manifest(manifest)
        old_text = self.manifest.read_text(encoding="utf-8")
        old = json.loads(old_text)
        archive = self.workspace / ("experiment-" + old["job_id"] + ".json")
        # Archive first; an interrupted publication fails closed on the ledger.
        with archive.open("x", encoding="utf-8") as output:
            os.chmod(archive, 0o600)
            output.write(old_text)
            output.flush()
            os.fsync(output.fileno())
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.workspace,
                                             delete=False) as output:
                temporary = Path(output.name)
                os.chmod(temporary, 0o600)
                json.dump(manifest, output, sort_keys=True)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.manifest)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def _record(self):
        record, job = super()._record()
        if (job.request.prompt != EDIT_PROMPT or job.request.complete_prompt is not True
                or not job.request.second_reference_image_uri or not job.request.preserve_audio
                or record.get("media", {}).get("video_sha256") != VIDEO_SHA256
                or (Path(record["reference"]).name,
                    Path(record.get("second_reference", "")).name) != REFERENCE_FILENAMES):
            raise SafetyError("Test 3 record does not match the reviewed experiment.")
        self.service.providers["kling"].experiment_id = job.idempotency_key
        return record, job


def main(argv=None):
    return run_cli(argv, runner_class=Test3Runner, default_prompt=EDIT_PROMPT, allow_new_plan=True)


if __name__ == "__main__":
    raise SystemExit(main())
