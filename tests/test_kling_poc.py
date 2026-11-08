"""Offline PoC tests: fake HTTPS responses, fake media probes, temporary ledgers."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import ssl
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from character_swap.kling_http import (
    API_HOST, TASK_PATH, KlingHTTPTransport, MediaDownloader, _PinnedHTTPSConnection, media_url,
)
from character_swap.kling_poc import CONFIRMATION, EXPERIMENT_ID, POCRunner, main, summary
from character_swap.poc_media import digest, probe_media, validate_media
from character_swap.store import SafetyError


class Response:
    def __init__(self, body=b'{"code":0}', status=200):
        self.status = status
        self.body = io.BytesIO(body)

    def read(self, size):
        return self.body.read(size)


class Connections:
    def __init__(self, response=None, error=None):
        self.response = response if response is not None else Response()
        self.error = error
        self.opened = []
        self.requests = []
        self.closed = 0

    def __call__(self, host, **kwargs):
        self.opened.append((host, kwargs))
        return self

    def request(self, method, path, body=None, headers=None):
        self.requests.append((method, path, body, headers))
        if self.error:
            raise self.error

    def getresponse(self):
        return self.response

    def close(self):
        self.closed += 1


class HTTPSBoundaryTests(unittest.TestCase):
    def test_api_transport_is_disabled_without_reading_key_or_opening_connection(self):
        key, connections = Mock(), Connections()
        transport = KlingHTTPTransport(key, connection_factory=connections)
        with self.assertRaises(SafetyError):
            transport.post(TASK_PATH, json={})
        key.assert_not_called()
        self.assertEqual(connections.opened, [])

    def test_fixed_host_verified_tls_authentication_and_json(self):
        connections = Connections()
        transport = KlingHTTPTransport(lambda: "offline-placeholder", enabled=True, connection_factory=connections)
        self.assertEqual(transport.post(TASK_PATH, json={"model_name": "kling-v3-omni"}), {"code": 0})
        host, options = connections.opened[0]
        self.assertEqual(host, API_HOST)
        self.assertEqual(options["timeout"], 30)
        self.assertTrue(options["context"].check_hostname)
        self.assertEqual(options["context"].verify_mode, ssl.CERT_REQUIRED)
        method, path, body, headers = connections.requests[0]
        self.assertEqual((method, path), ("POST", TASK_PATH))
        self.assertEqual(json.loads(body), {"model_name": "kling-v3-omni"})
        self.assertEqual(headers["Authorization"], "Bearer offline-placeholder")
        self.assertEqual(connections.closed, 1)

    def test_external_or_unsupported_paths_never_receive_credentials(self):
        key, connections = Mock(), Connections()
        transport = KlingHTTPTransport(key, enabled=True, connection_factory=connections)
        for path in ("https://example.com/", "//example.com/", TASK_PATH + "/unexpected"):
            with self.assertRaises(SafetyError):
                transport.post(path, json={})
        key.assert_not_called()
        self.assertEqual(connections.opened, [])

    def test_timeout_redirect_and_error_responses_never_retry_or_leak_details(self):
        for connections in (Connections(error=TimeoutError("private diagnostics")),
                            Connections(Response(status=302)), Connections(Response(status=429)),
                            Connections(Response(b"private invalid JSON"))):
            with self.subTest(connections=connections):
                transport = KlingHTTPTransport(lambda: "offline-placeholder", enabled=True, connection_factory=connections)
                with self.assertRaises(SafetyError) as raised:
                    transport.post(TASK_PATH, json={})
                self.assertNotIn("private", str(raised.exception))
                self.assertNotIn("offline-placeholder", str(raised.exception))
                self.assertEqual(len(connections.requests), 1)
                self.assertEqual(connections.closed, 1)

    def test_status_uses_get_without_post(self):
        connections = Connections()
        transport = KlingHTTPTransport(lambda: "offline-placeholder", enabled=True, connection_factory=connections)
        transport.get(TASK_PATH + "/task-1")
        self.assertEqual(connections.requests[0][0], "GET")

    def test_header_injection_is_rejected_before_connection(self):
        connections = Connections()
        transport = KlingHTTPTransport(lambda: "bad\r\nvalue", enabled=True, connection_factory=connections)
        with self.assertRaises(SafetyError):
            transport.get(TASK_PATH + "/task")
        self.assertEqual(connections.opened, [])

    def test_oversized_json_response_is_rejected(self):
        connections = Connections(Response(b"x" * (1024 * 1024 + 1)))
        transport = KlingHTTPTransport(lambda: "offline-placeholder", enabled=True, connection_factory=connections)
        with self.assertRaises(SafetyError):
            transport.get(TASK_PATH + "/task")
        self.assertEqual(len(connections.requests), 1)


class PollDiagnosticsTests(unittest.TestCase):
    def transport(self, payload, *, status=200, sink=None):
        connections = Connections(Response(json.dumps(payload).encode(), status))
        transport = KlingHTTPTransport(lambda: "offline-private-key", enabled=True,
                                       connection_factory=connections, poll_diagnostics=sink)
        return transport, connections

    def test_debug_poll_emits_only_four_allowlisted_fields(self):
        events = []
        payload = {"code": 0, "request_id": "req-123", "message": "private-message",
                   "data": {"task_id": "offline-task", "task_status": "submitted",
                            "task_result": {"videos": [{"url": "https://example.com/private?signature=secret"}]}},
                   "Authorization": "Bearer offline-private-key"}
        transport, connections = self.transport(payload, sink=events.append)
        self.assertEqual(transport.get(TASK_PATH + "/offline-task"), payload)
        self.assertEqual(events, [{"http_status": 200, "kling_response_code": 0,
                                  "provider_task_status": "submitted", "request_id": "req-123"}])
        self.assertNotIn("private", json.dumps(events))
        self.assertEqual([request[0] for request in connections.requests], ["GET"])

    def test_non_200_response_exposes_safe_code_without_following_redirect(self):
        for status in (401, 302):
            with self.subTest(status=status):
                events = []
                transport, connections = self.transport(
                    {"code": 1000, "request_id": "req-error", "message": "private credential diagnostics"},
                    status=status, sink=events.append,
                )
                with self.assertRaises(SafetyError):
                    transport.get(TASK_PATH + "/offline-task")
                self.assertEqual(events, [{"http_status": status, "kling_response_code": 1000,
                                          "provider_task_status": None, "request_id": "req-error"}])
                self.assertEqual(len(connections.requests), 1)

    def test_malicious_or_malformed_diagnostic_fields_are_not_echoed(self):
        for request_id in ("offline-private-key", "prefix-offline-private-key-suffix",
                           "https://example.com/private?signature=secret", "req\nprivate", {"private": "value"}):
            with self.subTest(request_id=request_id):
                events = []
                transport, _ = self.transport({
                    "code": "private response text", "request_id": request_id,
                    "data": {"task_status": "https://example.com/private?token=secret"},
                }, sink=events.append)
                transport.get(TASK_PATH + "/offline-task")
                self.assertEqual(events, [{"http_status": 200, "kling_response_code": None,
                                          "provider_task_status": "unrecognized", "request_id": None}])

    def test_invalid_json_and_transport_failure_report_available_metadata_only(self):
        for connections, expected_http in ((Connections(Response(b"private invalid body")), 200),
                                            (Connections(error=TimeoutError("private network error")), None)):
            events = []
            transport = KlingHTTPTransport(lambda: "offline-private-key", enabled=True,
                                           connection_factory=connections, poll_diagnostics=events.append)
            with self.assertRaises(SafetyError):
                transport.get(TASK_PATH + "/offline-task")
            self.assertEqual(events, [{"http_status": expected_http, "kling_response_code": None,
                                      "provider_task_status": None, "request_id": None}])
            self.assertNotIn("private", json.dumps(events))

    def test_diagnostics_are_optional_and_never_emitted_for_post(self):
        transport, _ = self.transport({"code": 0})
        transport.get(TASK_PATH + "/offline-task")
        sink = Mock()
        transport, _ = self.transport({"code": 0}, sink=sink)
        transport.post(TASK_PATH, json={})
        sink.assert_not_called()

    def test_diagnostic_sink_failure_does_not_change_response(self):
        transport, _ = self.transport({"code": 0}, sink=Mock(side_effect=RuntimeError("output failed")))
        self.assertEqual(transport.get(TASK_PATH + "/offline-task"), {"code": 0})


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.destination = Path(self.temp.name) / "result.mp4"
        self.resolver = Mock(return_value=[(2, 1, 6, "", ("93.184.216.34", 443))])

    def test_downloader_defaults_off_without_dns(self):
        downloader = MediaDownloader(resolver=self.resolver)
        with self.assertRaises(SafetyError):
            downloader.download("https://example.com/result", self.destination, max_bytes=100)
        self.resolver.assert_not_called()

    def test_download_has_no_authorization_and_pins_public_address(self):
        connections = Connections(Response(b"offline-output"))
        downloader = MediaDownloader(enabled=True, resolver=self.resolver, connection_factory=connections)
        downloader.download("https://example.com/video?signature=placeholder", self.destination, max_bytes=100)
        self.assertEqual(self.destination.read_bytes(), b"offline-output")
        self.assertEqual(connections.opened[0][1]["address"], "93.184.216.34")
        self.assertTrue(connections.opened[0][1]["context"].check_hostname)
        self.assertEqual(connections.requests[0],
                         ("GET", "/video?signature=placeholder", None, {"Accept": "*/*"}))

    def test_private_addresses_are_blocked_before_connection(self):
        connections = Connections()
        self.resolver.return_value = [(2, 1, 6, "", ("127.0.0.1", 443))]
        downloader = MediaDownloader(enabled=True, resolver=self.resolver, connection_factory=connections)
        with self.assertRaises(SafetyError):
            downloader.download("https://example.com/video", self.destination, max_bytes=100)
        self.assertEqual(connections.opened, [])

    def test_pinned_connection_still_verifies_original_hostname(self):
        context, raw_socket = Mock(), Mock()
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
        connection = _PinnedHTTPSConnection("example.com", address="93.184.216.34",
                                            timeout=30, context=context)
        with patch("character_swap.kling_http.socket.create_connection", return_value=raw_socket) as connect:
            connection.connect()
        connect.assert_called_once_with(("93.184.216.34", 443), 30)
        context.wrap_socket.assert_called_once_with(raw_socket, server_hostname="example.com")

    def test_redirect_oversize_and_empty_media_are_rejected(self):
        for response in (Response(status=302), Response(b"x" * 101), Response(b"")):
            connections = Connections(response)
            downloader = MediaDownloader(enabled=True, resolver=self.resolver, connection_factory=connections)
            with self.assertRaises(SafetyError):
                downloader.download("https://example.com/video", self.destination, max_bytes=100)
            self.assertFalse(self.destination.exists())
            self.assertEqual(len(connections.requests), 1)

    def test_existing_output_is_not_overwritten(self):
        self.destination.write_bytes(b"existing")
        downloader = MediaDownloader(enabled=True, resolver=self.resolver,
                                     connection_factory=Connections(Response(b"new")))
        with self.assertRaises(SafetyError):
            downloader.download("https://example.com/video", self.destination, max_bytes=100)
        self.assertEqual(self.destination.read_bytes(), b"existing")

    def test_invalid_media_url_schemes_credentials_and_ports_are_rejected(self):
        for url in ("http://example.com/video", "file:///video", "https://user:password@example.com/video",
                    "https://example.com:444/video", "https://example.com/video#fragment", "https://example.com/a b"):
            with self.subTest(url=url), self.assertRaises(SafetyError):
                media_url(url)


def video_metadata():
    return {
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "5.000000", "start_time": "0"},
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720,
             "avg_frame_rate": "24/1", "r_frame_rate": "24/1", "nb_read_frames": "120",
             "duration": "5.000000", "start_time": "0"},
            {"codec_type": "audio", "codec_name": "aac", "duration": "5.000000", "start_time": "0", "nb_read_frames": "235"},
        ],
    }


class MediaValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.video = Path(self.temp.name) / "video.mp4"
        self.image = Path(self.temp.name) / "reference.png"
        self.video.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"offline")
        self.image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"offline")
        self.info = video_metadata()
        self.picture = {"streams": [{"codec_name": "png", "width": 600, "height": 900}]}
        self.probe = lambda path: self.info if Path(path).suffix == ".mp4" else self.picture
        self.decoder = Mock(return_value=(120, (1280, 720), (600, 900)))

    def validate(self):
        return validate_media(self.video, self.image, probe=self.probe, decoder=self.decoder)

    def test_valid_five_second_video_and_audio_with_matching_decode(self):
        self.assertEqual(self.validate(), {"video_sha256": digest(self.video),
                                          "image_sha256": digest(self.image), "duration_seconds": 5})

    def test_missing_inspector_fails_closed_without_running_subprocess(self):
        with patch("character_swap.poc_media.shutil.which", return_value=None), \
             patch("character_swap.poc_media.subprocess.run") as process:
            with self.assertRaisesRegex(SafetyError, "ffprobe"):
                probe_media(self.video)
            process.assert_not_called()

    def test_probe_uses_local_only_protocol_and_timeout(self):
        completed = Mock(stdout=json.dumps(self.info).encode(), stderr=b"")
        with patch("character_swap.poc_media.shutil.which", return_value="offline-ffprobe"), \
             patch("character_swap.poc_media.subprocess.run", return_value=completed) as process:
            self.assertEqual(probe_media(self.video), self.info)
            args = process.call_args.args[0]
            self.assertEqual(args[args.index("-protocol_whitelist") + 1], "file")
            self.assertEqual(process.call_args.kwargs["timeout"], 60)

    def test_duration_frame_count_fps_and_audio_must_all_match(self):
        variants = []
        for field, value in (("duration", "6"), ("start_time", "1"), ("avg_frame_rate", "30/1"),
                             ("r_frame_rate", "30/1"), ("nb_read_frames", "119")):
            data = video_metadata()
            data["streams"][0][field] = value
            variants.append(data)
        data = video_metadata()
        data["streams"][1]["duration"] = "4.9"
        variants.append(data)
        data = video_metadata()
        data["streams"] = data["streams"][:1]
        variants.append(data)
        for data in variants:
            self.info = data
            with self.subTest(data=data), self.assertRaises(SafetyError):
                self.validate()

    def test_dimensions_aspect_ratio_and_decoding_are_verified(self):
        self.picture["streams"][0]["width"] = 100
        with self.assertRaises(SafetyError):
            self.validate()
        self.picture["streams"][0]["width"] = 600
        self.decoder.return_value = (119, (1280, 720), (600, 900))
        with self.assertRaises(SafetyError):
            self.validate()

    def test_wrong_signature_is_rejected_before_probe(self):
        self.image.write_bytes(b"not-png")
        probe = Mock()
        with self.assertRaises(SafetyError):
            validate_media(self.video, self.image, probe=probe)
        probe.assert_not_called()

    def test_file_sizes_are_checked_before_probe(self):
        with patch("character_swap.poc_media.VIDEO_LIMIT", 1):
            with self.assertRaises(SafetyError):
                self.validate()
        with patch("character_swap.poc_media.IMAGE_LIMIT", 1):
            with self.assertRaises(SafetyError):
                self.validate()

    def test_unknown_audio_duration_or_unsupported_format_is_rejected(self):
        self.info["streams"][1]["duration"] = "N/A"
        with self.assertRaises(SafetyError):
            self.validate()
        self.info = video_metadata()
        self.info["streams"][1]["nb_read_frames"] = "0"
        with self.assertRaises(SafetyError):
            self.validate()
        self.info = video_metadata()
        self.info["streams"][0]["codec_name"] = "unknown"
        with self.assertRaises(SafetyError):
            self.validate()


class FakeTransport:
    def __init__(self):
        self.posts = []
        self.gets = []
        self.error = None
        self.response = {"code": 0, "data": {"task_id": "offline-task", "task_status": "submitted"}}

    def post(self, path, *, json):
        self.posts.append((path, json))
        if self.error:
            raise self.error
        return self.response

    def get(self, path):
        self.gets.append(path)
        return self.response


class FakeDownloader:
    def __init__(self, contents):
        self.contents = contents
        self.calls = []

    def download(self, url, destination, *, max_bytes):
        self.calls.append((url, max_bytes))
        Path(destination).write_bytes(self.contents[url])
        return Path(destination)


class RunnerSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.video = self.root / "video.mp4"
        self.image = self.root / "reference.png"
        self.video.write_bytes(b"offline-video")
        self.image.write_bytes(b"offline-image")
        self.video_url = "https://example.com/video?signature=placeholder"
        self.image_url = "https://example.com/image?signature=placeholder"
        self.transport = FakeTransport()
        self.downloader = FakeDownloader({self.video_url: self.video.read_bytes(), self.image_url: self.image.read_bytes()})
        self.validator = lambda video, image: {"video_sha256": digest(video), "image_sha256": digest(image), "duration_seconds": 5}
        self.workspace = self.root / "experiment"

    def runner(self, enabled=False, **changes):
        values = {"enabled": enabled, "transport": self.transport, "downloader": self.downloader,
                  "validator": self.validator, "sleep": lambda _: None}
        values.update(changes)
        return POCRunner(self.workspace, **values)

    def plan(self, runner=None):
        return (runner or self.runner()).plan(self.video, self.image, self.video_url, self.image_url)

    def submit(self, runner):
        return runner.submit(CONFIRMATION, accept_estimated_billing=True)

    def test_offline_plan_and_status_do_not_call_providers_or_storage(self):
        runner = self.runner()
        job = self.plan(runner)
        self.assertEqual(runner.status().id, job.id)
        for action in (lambda: self.submit(runner), runner.poll, runner.download):
            with self.assertRaises(SafetyError):
                action()
        self.assertEqual(self.transport.posts + self.transport.gets + self.downloader.calls, [])
        self.assertEqual(job.reserved_units, 0)

    def test_confirmation_and_billing_acceptance_are_both_required(self):
        self.plan()
        runner = self.runner(True)
        for confirmation, acceptance in ((None, False), (CONFIRMATION, False), ("wrong", True)):
            with self.assertRaises(SafetyError):
                runner.submit(confirmation, accept_estimated_billing=acceptance)
        self.assertEqual(self.transport.posts + self.downloader.calls, [])

    def test_one_submission_has_expected_payload_and_six_unit_reservation(self):
        job = self.plan()
        runner = self.runner(True)
        result = self.submit(runner)
        self.submit(runner)
        self.assertEqual(result.id, job.id)
        self.assertEqual(result.reserved_units, 6000)
        self.assertEqual(len(self.transport.posts), 1)
        payload = self.transport.posts[0][1]
        self.assertEqual(payload["model_name"], "kling-v3-omni")
        self.assertEqual(payload["mode"], "pro")
        self.assertEqual(payload["external_task_id"], EXPERIMENT_ID)
        self.assertEqual(payload["video_list"][0]["refer_type"], "base")
        self.assertEqual(payload["video_list"][0]["keep_original_sound"], "yes")
        self.assertEqual(payload["sound"], "off")
        self.assertNotIn("duration", payload)

    def test_restart_reuses_fixed_job_and_cannot_submit_twice(self):
        first = self.plan()
        self.submit(self.runner(True))
        self.assertEqual(self.plan().id, first.id)
        self.submit(self.runner(True))
        self.assertEqual(len(self.transport.posts), 1)

    def test_concurrent_runner_invocations_submit_only_once(self):
        self.plan()
        with ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(lambda _: self.submit(self.runner(True)), range(3)))
        self.assertEqual(len({result.id for result in results}), 1)
        self.assertEqual(len(self.transport.posts), 1)
        self.assertEqual(self.runner().status().reserved_units, 6000)

    def test_crash_after_claim_is_not_replayed_on_restart(self):
        job = self.plan()
        runner = self.runner(True)
        runner.service.authorize(job.id)
        runner.service.store.claim(job.id, ("authorized",), "submitting", 6000, 6000)
        self.assertEqual(self.submit(self.runner(True)).status, "submitting")
        self.assertEqual(self.transport.posts, [])
        self.assertEqual(self.downloader.calls, [])

    def test_timeout_preserves_reservation_and_never_resubmits(self):
        self.plan()
        self.transport.error = TimeoutError("private diagnostics")
        result = self.submit(self.runner(True))
        self.assertEqual(result.status, "submission_unknown")
        self.assertEqual(result.reserved_units, 6000)
        self.submit(self.runner(True))
        with self.assertRaises(SafetyError):
            self.runner(True).poll()
        self.assertEqual(len(self.transport.posts), 1)

    def test_malformed_receipt_also_blocks_resubmission(self):
        self.plan()
        self.transport.response = {"code": 0, "data": {}}
        self.assertEqual(self.submit(self.runner(True)).status, "submission_unknown")
        self.submit(self.runner(True))
        self.assertEqual(len(self.transport.posts), 1)

    def test_changed_local_or_hosted_media_blocks_post(self):
        self.plan()
        self.video.write_bytes(b"changed")
        with self.assertRaises(SafetyError):
            self.submit(self.runner(True))
        self.video.write_bytes(b"offline-video")
        self.downloader.contents[self.video_url] = b"different hosted bytes"
        with self.assertRaises(SafetyError):
            self.submit(self.runner(True))
        self.assertEqual(self.transport.posts, [])

    def test_changed_request_cannot_replace_the_fixed_experiment(self):
        self.plan()
        with self.assertRaises(SafetyError):
            self.runner().plan(self.video, self.image, self.video_url, self.image_url, "Different instructions")

    def test_validator_failure_cannot_create_or_submit_a_job(self):
        runner = self.runner(validator=Mock(side_effect=SafetyError("inspection unavailable")))
        with self.assertRaises(SafetyError):
            self.plan(runner)
        self.assertFalse(runner.manifest.exists())
        self.assertEqual(self.transport.posts, [])

    def test_poll_resumes_saved_task_and_never_posts(self):
        self.plan()
        self.submit(self.runner(True))
        self.transport.response = {"code": 0, "data": {
            "task_id": "offline-task", "task_status": "succeed",
            "task_result": {"videos": [{"url": "https://example.com/output"}]},
        }}
        result = self.runner(True).poll(count=2)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(self.transport.gets, [TASK_PATH + "/offline-task"])
        self.assertEqual(len(self.transport.posts), 1)

    def test_output_download_does_not_use_api_transport_or_key(self):
        self.plan()
        result = self.submit(self.runner(True))
        runner = self.runner(True)
        runner.service.store.update(result, status="succeeded", video_url="https://example.com/output")
        self.downloader.contents["https://example.com/output"] = b"offline-output"
        path = runner.download()
        self.assertEqual(path.read_bytes(), b"offline-output")
        self.assertEqual(self.transport.gets, [])
        self.assertEqual(len(self.transport.posts), 1)

    def test_public_summary_omits_request_and_private_urls(self):
        job = self.plan()
        output = json.dumps(summary(job))
        self.assertNotIn("https://", output)
        self.assertNotIn("signature", output)
        self.assertNotIn("request", output)

    def test_cli_without_live_never_prompts_for_api_key(self):
        self.plan()
        original = POCRunner
        def factory(**kwargs):
            return original(self.workspace, validator=self.validator, **kwargs)
        with patch("character_swap.kling_poc.POCRunner", side_effect=factory), \
             patch("character_swap.kling_poc.masked_input", side_effect=AssertionError("No credential input allowed")), \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main([]), 0)
            self.assertEqual(main(["submit"]), 2)
            self.assertEqual(main(["poll"]), 2)
            self.assertEqual(main(["download"]), 2)
        self.assertNotIn("https://", output.getvalue())

    def test_cli_debug_poll_uses_saved_task_and_never_submits(self):
        self.plan()
        saved = self.submit(self.runner(True))  # Fake submission in a temporary ledger.
        original_runner, original_transport = POCRunner, KlingHTTPTransport
        payload = {"code": 0, "request_id": "req-cli", "data": {
            "task_id": saved.provider_task_id, "task_status": "submitted",
        }}
        connections = Connections(Response(json.dumps(payload).encode()))
        def runner_factory(**kwargs):
            return original_runner(self.workspace, validator=self.validator, **kwargs)
        def transport_factory(supplier, **kwargs):
            return original_transport(lambda: "offline-private-key", connection_factory=connections, **kwargs)
        with patch("character_swap.kling_poc.POCRunner", side_effect=runner_factory), \
             patch("character_swap.kling_poc.KlingHTTPTransport", side_effect=transport_factory), \
             patch("character_swap.kling_poc.masked_input", side_effect=AssertionError("No real key input")), \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["poll", "--live", "--debug", "--count", "1"]), 0)
        diagnostics = json.loads(output.getvalue().splitlines()[0].split(": ", 1)[1])
        self.assertEqual(diagnostics, {"http_status": 200, "kling_response_code": 0,
                                      "provider_task_status": "submitted", "request_id": "req-cli"})
        self.assertEqual(connections.requests[0][0:2], ("GET", TASK_PATH + "/" + saved.provider_task_id))
        self.assertEqual(len(connections.requests), 1)
        self.assertEqual(self.runner().status().provider_task_id, saved.provider_task_id)
        self.assertEqual(self.runner().status().reserved_units, 6000)
        self.assertNotIn("offline-private-key", output.getvalue())
        self.assertNotIn("https://", output.getvalue())

    def test_cli_debug_does_not_bypass_live_requirement(self):
        self.plan()
        original_runner = POCRunner
        def runner_factory(**kwargs):
            return original_runner(self.workspace, validator=self.validator, **kwargs)
        with patch("character_swap.kling_poc.POCRunner", side_effect=runner_factory), \
             patch("character_swap.kling_poc.masked_input", side_effect=AssertionError("No key input")), \
             patch("socket.create_connection", side_effect=AssertionError("No network")), \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["poll", "--debug"]), 2)
        self.assertNotIn("Kling poll diagnostics:", output.getvalue())


if __name__ == "__main__":
    unittest.main()
