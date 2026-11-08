# AI Video Studio

AI Video Studio is a Python/Flask proof of concept for Runway text-to-video generation and reference-guided performer replacement in existing video. Character Swap experiments use separate command-line tools, SQLite job ledgers, offline media validation, and explicit spending authorization.

This is a development prototype, not a production service or a guarantee of identity, choreography, or scene preservation. Private demonstration media is intentionally excluded from Git.

## Current checkpoint

| Component | Status |
| --- | --- |
| Runway text-to-video | Existing Flask generation, status, gallery, and deletion routes are implemented. This checkpoint was validated offline only. |
| Legacy Flask Character Swap | Existing Runway upload/preparation/generation routes remain separate from Kling. Preparation and generation return HTTP 403 in demo mode. |
| Kling 3.0 Omni demonstration | An authorized five-second, two-image performer-replacement experiment completed and was downloaded. The operator reported excellent Test 3 results. |
| Full-video preprocessing | A source was converted from 30 fps to constant 24 fps without changing playback speed: 598 frames, 24.916667 seconds, 1920x1080. |
| Five-segment experiment | Five inputs are prepared. Segments 01 and 02 succeeded and were downloaded. Segments 03-05 have not been submitted and have no jobs in the inspected ledger. |
| Preview assembly | A local preview exists. A black-video stitching/playback bug is reported and remains unresolved; assembly is not a validated delivery workflow. |
| Kling 4.0 | Separate request serialization, receipt normalization, GET-only polling, result extraction, and usage/billing reporting are tested offline. Live submission is blocked. |
| Original-audio restoration | Local FFmpeg restoration is implemented and tested with synthetic media. No actual full-video Kling 4.0 result has been restored. |

## Architecture

```text
app.py                         Flask create_app() factory and existing routes
templates/                     Text-to-video and legacy Character Swap interfaces
character_swap/
  providers/base.py            Shared request, status, result, and error types
  providers/kling.py           Legacy Kling 3.0 Omni adapter
  providers/kling4.py          Separate 4.0 serializer and guarded provider
  store.py                     SQLite jobs, ownership, reservations, atomic claims
  service.py                   Authorization and provider orchestration
  web.py                       Session-owned, read-only local job lookup
  kling_http.py                Legacy HTTPS transport and private media downloader
  poc_media.py                 Strict five-second media inspection
  kling_poc*.py                Isolated Tests 1, 2, and 3
  kling_full_video.py          Five segment jobs sharing one spending ledger
  kling4_plan.py               Offline full-video validation and planning
  kling4_http.py               Explicitly enabled, GET-only task transport
  kling4_results.py            URL-free task, usage, and billing reports
  kling4_audio.py              Verified original-audio restoration
tests/                         Offline Flask, provider, safety, and media tests
uploads/                       Ignored private media and experiment workspaces
```

Importing `app.py` does not load dotenv, initialize databases, or construct a Runway client. Calling `create_app()` initializes the application explicitly. Offline tests inject fake clients, disabled policies, and temporary database/upload paths.

Text-to-video history uses the `videos` table. Character Swap uses a separate `character_swap_jobs` table. CLI experiments use isolated database files; all five full-video jobs share one ledger.

The read-only Flask job endpoint uses signed anonymous-session ownership. Unknown, ownerless, and other-session jobs return the same 404; missing/insecure signing keys disable lookup with 503. This does not establish authentication or ownership for the older gallery and generation routes.

## Kling 3.0 Omni demonstration

The successful Test 3 uses `kling-v3-omni` in Pro mode with the original video as the base editing input. Reference image 1 supplies identity, body, hairstyle, and clothing; image 2 supplements facial identity. The prompt requests preservation of choreography, camera movement, environment, lighting, other performers, timing, and original audio.

Legacy settings are `refer_type="base"`, `keep_original_sound="yes"`, `sound="off"`, and `multi_shot=false`. These are not reused as Kling 4.0 settings.

Tests 1-3 have distinct workspaces. Test 3 can explicitly supersede an unsubmitted, unreserved plan while preserving its record and archived manifest. Reserved, attempted, and ambiguous submissions cannot be replaced this way.

### Preprocessing and segment status

The 24 fps testing copy is 16.667 milliseconds shorter than the original 30 fps source because of complete-frame rounding. Playback speed was unchanged; AAC audio was stream-copied.

Frame-accurate cuts required lossless H.264 re-encoding because keyframes did not coincide with the requested boundaries. Decoded segment frames were compared with the testing copy: all 598 frames are covered once, in order.

| Input | Source interval | Frames | Generation status |
| --- | --- | --- | --- |
| Segment 01 | 0-5 seconds | 120 | Succeeded; downloaded |
| Segment 02 | 5-10 seconds | 120 | Succeeded; downloaded |
| Segment 03 | 10-15 seconds | 120 | Not submitted |
| Segment 04 | 15-20 seconds | 120 | Not submitted |
| Segment 05 | 20-24.916667 seconds | 118 | Not submitted |

The first two generated outputs each contain 121 frames, rather than 120. A preview retaining both outputs has 242 frames over 10.083333 seconds. Frame preservation and successful FFmpeg decoding do not prove correct browser rendering. The reported black-video stitching bug needs reproduction, codec/player compatibility checks, and visual acceptance testing. No reusable, validated stitching command is included.

The original continuous audio is retained locally for final assembly. Do not infer generated timing from input duration or concatenate generated audio tracks for the final soundtrack.

## Installation and configuration

The checkpoint was tested with Python 3.12.7. Python dependencies are listed in `requirements.txt` and are currently unpinned. FFmpeg and ffprobe are separate tools; install verified builds from a trusted source. The application never downloads them or changes the permanent PATH.

```powershell
git clone https://github.com/LeonTolentino30/AI-Video-Studio.git
cd AI-Video-Studio
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Configure these privately in an ignored local `.env` or server environment:

| Variable | Purpose |
| --- | --- |
| `MOCK_MODE=true` | Default demo mode. Keep enabled for local interface work. |
| `RUNWAYML_API_SECRET` | Required by the current default startup path, even in demo mode. Never commit its value. |
| `FLASK_SECRET_KEY` | Stable, securely generated signing key. Use at least 32 random bytes and do not regenerate each restart. |
| `CHARACTER_SWAP_HTTPS=false` | Explicit Secure-cookie opt-out for HTTP-only local development. Use `true` for HTTPS deployments. |

Never paste secrets or signed URLs into issues, command arguments, screenshots, or documentation. Kling CLIs obtain credentials through hidden terminal prompts, not `.env`; a compatible interactive terminal is required.

```powershell
.\.venv\Scripts\python.exe app.py
```

Open `http://127.0.0.1:5000`. The script binds to all interfaces (`0.0.0.0`) with debug disabled. Use it only in a trusted development environment, not as a public production server.

There is no global `app` object for an `app:app` entry point. Deployment tooling must use the `create_app()` factory and production configuration. Application initialization creates local directories and SQLite tables; module import does not.

### Flask routes

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | Text-to-video interface |
| GET | `/config` | Demo/real mode flags |
| POST | `/generate` | Existing Runway text-to-video generation |
| GET | `/status/<task_id>` | Existing Runway status lookup |
| GET | `/gallery` | Existing local gallery |
| DELETE | `/gallery/<video_id>` | Delete a gallery record |
| GET | `/character-swap` | Legacy Character Swap interface |
| POST | `/character-swap/upload` | Local media upload/inspection |
| POST | `/character-swap/prepare` | Runway preparation; blocked in demo mode |
| POST | `/character-swap/generate` | Runway generation; blocked in demo mode |
| GET | `/character-swap/status/<task_id>` | Legacy Runway status lookup |
| GET | `/character-swap/jobs/<job_id>` | Owner-scoped local metadata; no provider calls |

Flask does not expose the Kling CLI submission commands. Its new local job service has no registered providers and spending is disabled. That guard does not cover the separate legacy generation routes when demo mode is deliberately switched off.

## Offline testing and CLI tools

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests
```

At this checkpoint, **250 tests passed**, including a real offline FFmpeg restoration round trip using synthetic temporary media. That integration test skips if existing tools are unavailable. Other tests use fake transports/probes and temporary SQLite databases, without production credentials or paid requests.

For real local media validation, make verified tools available to the current terminal only:

```powershell
$env:PATH = "C:\path\to\verified-ffmpeg\bin;$env:PATH"
```

Inspect options without enabling live operations:

```powershell
.\.venv\Scripts\python.exe -B -m character_swap.kling_poc_test3 --help
.\.venv\Scripts\python.exe -B -m character_swap.kling_full_video --help
.\.venv\Scripts\python.exe -B -m character_swap.kling4_plan --help
.\.venv\Scripts\python.exe -B -m character_swap.kling4_audio --help
```

The five-segment runner supports independent `plan`, `submit`, `poll`, and `download` commands with `--segment 01` through `05`. Planning is offline. Submission needs `--live` and the exact per-segment spending confirmation. Polling and downloading also require explicit live enabling and can make read-only network requests. Never use live commands as tests.

Before a paid POST, the full-video runner verifies S3 media bytes against pinned hashes. Signed URLs and credentials remain in memory; the ledger stores hash identifiers and non-downloadable result markers. Older Tests 1-3 persist signed input URLs in their private ledgers, which must never be committed or pasted into reports.

The CLIs are pinned to approved media hashes and local workspaces. A fresh clone has no private assets and cannot reproduce the demonstration without separately authorized media and deliberate configuration review. Never reset a ledger, change experiment IDs, or replace hashes to bypass a failed or uncertain submission.

## Kling 4.0 and original audio

The separate 4.0 adapter serializes `contents` with a prompt, one `base_video`, and two ordered `refer_image` objects using custom `@id` references. Requested settings are 1080p and adaptive aspect ratio.

Receipts use `data.id` and `data.status`. GET-only polling uses `/tasks?task_ids=...`, matches the requested ID in the returned array, and handles `submitted`, `processing`, `succeeded`, and `failed`. Results are selected by output type. Malformed responses fail closed, and success without a valid video URL is not accepted as successful completion.

Usage and billing may be absent until completion. Reports distinguish actual video duration from normalized input duration and billed output duration, preserve decimal amounts, and do not combine currencies or package units. Protected URLs never appear in normalized results/reports; they remain in memory for immediate private downloading. The documented retention period is 30 days.

`kling4_plan` exposes offline planning only. Live submission remains blocked pending original-audio behavior, compatible account/package access, and explicit spending authorization. A serializer and mocked polling tests are not evidence of a successful live Kling 4.0 edit.

`kling4_audio` restores original audio locally for a future downloaded result:

- Copies generated video packets and verifies decoded frame hashes and timing.
- Uses the hash-verified original soundtrack from time zero, encoded as AAC stereo at 48 kHz.
- Checks duration, decoding, audio similarity, and unchanged input hashes.
- Requires `--allow-tail-adjustment` approval for audio-only padding/trimming up to 0.25 seconds. Larger mismatches are rejected; playback speed never changes.
- Publishes a separate MP4 only after verification, without overwriting inputs or an existing output.

This cannot repair changed choreography, model-generated retiming, or lip-sync errors. Listening and visual review remain necessary. Atomic publication requires a filesystem supporting local hard links.

## Security, API costs, and private artifacts

- All personal video, photos, results, audio, manifests, and private ledgers remain in ignored local `uploads/` workspaces. Their contents, private paths, task IDs, and signed URLs are not published here.
- No new media or screenshots are included in this checkpoint. Publication needs separate approval of exact files, ownership/publication rights, likeness consent, and size checks. Local media is not backed up to GitHub.
- Never commit API/AWS credentials, `.env`, virtual environments, databases/journals, logs, signed URLs, or raw provider responses. Ignore rules do not replace staged-diff review.
- Signed S3 URLs are bearer capabilities. Refresh them privately at execution time, not through command arguments. Do not forward API Authorization headers to media downloads.
- Spending defaults off. SQLite transactions enforce reservations and one submission claim per job. Ambiguous timeouts retain reservations and do not retry automatically. Estimates are not actual bills and do not cap provider activity outside the local ledger.
- The five-segment workflow reserves an estimated 6 API units per segment and 30 total. Kling 4.0 planning uses a provisional 45-unit estimate for this approximately 25-second 1080p edit; submission remains disabled. Older General packages are not interchangeable with 4.0 Flagship packages. Verify current pricing, normalization, rounding, access, and balance before authorization.
- Legacy Flask generation/gallery routes are not ready for public multi-user exposure. Anonymous job isolation is not full account authentication. Rate limits, complete media ownership enforcement, quotas, and deployment hardening remain unfinished.

Official references: [legacy Omni API](https://kling.ai/document-api/api/video/o1/video-omni/legacy), [4.0 Omni API](https://kling.ai/document-api/api/video/4-0/video-omni), [pricing](https://kling.ai/dev/pricing). Provider behavior and pricing can change; code safeguards do not authorize a purchase.

## Roadmap

- Reproduce and fix black-video stitching/playback; validate in target players.
- Review joins for identity, clothing, lighting, background, and timing discontinuities.
- Complete Segments 03-05 only with separate authorization after quality/spending review.
- Resolve remaining 4.0 audio/account requirements before any live full-video submission.
- Validate original-audio restoration against a real, authorized full-video result.
- Improve dependency reproducibility, deployment, authentication, and ownership enforcement.

**Future concept, not implemented:** AI virtual try-on for clothing sellers using authorized garment images and consenting adult model references. No seller workflow, garment-fitting engine, commerce integration, customer-media pipeline, or virtual try-on API is implemented.

## License

No open-source license has been selected. No rights to private demonstration music, videos, photographs, or likenesses are granted by this repository.