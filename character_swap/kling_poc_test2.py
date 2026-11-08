"""Test 2: same media/settings, revised singing-mouth identity instructions.

Uses a separate fixed ledger and output folder. The Test 1 entry point and
workspace remain unchanged. Planning is offline; submission still requires
the original explicit live/spending confirmations and six-unit reservation.
"""

from pathlib import Path

from .kling_poc import POCRunner, WORKSPACE as TEST1_WORKSPACE, main as run_cli
from .poc_media import digest
from .store import SafetyError


EXPERIMENT_ID = "kling-omni-pro-5s-v2"
WORKSPACE = TEST1_WORKSPACE.with_name("kling-omni-poc-test2")
VIDEO_SHA256 = "78411e5be379e72b9b2f192fd12b17cebe56b7440ccf8a608be35238a4b6c80d"
REFERENCE_SHA256 = "513b577b7dd49bc8682048e3815b70119f3c60330bf899b00658bd11b7af0f8c"
EDIT_PROMPT = (
    "The main performer is centered in the frame. "
    "While singing, preserve the adult in <<<image_1>>>'s facial identity: "
    "natural mouth width relative to the face, upper-to-lower lip proportions, "
    "lip thickness, lip-corner shape and placement, mustache shape and density, "
    "and jawline. Animate the mouth naturally for the original singing timing "
    "and expressions while retaining these reference-specific proportions. "
    "Keep the mustache consistent around moving lips; avoid widening the mouth, "
    "stretching lip corners, exaggerating lip volume, or reshaping the jaw. "
    "Preserve the original choreography, background, camera movement, lighting, "
    "supporting performers, audio, and timing."
)


class Test2Runner(POCRunner):
    experiment_id = EXPERIMENT_ID

    def __init__(self, workspace=WORKSPACE, **kwargs):
        if Path(workspace).resolve() == TEST1_WORKSPACE.resolve():
            raise SafetyError("Test 2 cannot use the Test 1 workspace.")
        super().__init__(workspace, **kwargs)

    def plan(self, video, reference, video_url, image_url, prompt=EDIT_PROMPT):
        if prompt != EDIT_PROMPT:
            raise SafetyError("Test 2 uses the reviewed mouth-accuracy prompt only.")
        if digest(video) != VIDEO_SHA256 or digest(reference) != REFERENCE_SHA256:
            raise SafetyError("Test 2 requires the unchanged validated Test 1 video and reference photo.")
        return super().plan(video, reference, video_url, image_url, prompt)


def main(argv=None):
    return run_cli(argv, runner_class=Test2Runner, default_prompt=EDIT_PROMPT)


if __name__ == "__main__":
    raise SystemExit(main())
