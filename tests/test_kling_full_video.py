"""Offline-only isolated-ledger, media, and sensitive-data regressions."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import character_swap.kling_full_video as full
from character_swap.kling_poc_test3 import EDIT_PROMPT
from character_swap.poc_media import digest
from character_swap.providers.base import CharacterEditRequest
from character_swap.store import SafetyError
import test_kling_poc as fixtures


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        (self.base / 'segments').mkdir()
        self.references = self.base / 'photos'
        self.references.mkdir()
        for i in range(1, 6):
            (self.base / 'segments' / f'segment_{i:02d}.mp4').write_bytes(f'video{i}'.encode())
        (self.references / 'test1.png').write_bytes(b'neutral')
        (self.references / 'test2.jpeg').write_bytes(b'smiling')
        (self.base / 'segments' / 'original_continuous_audio.m4a').write_bytes(b'continuous audio')
        self.transport = fixtures.FakeTransport()
        self.transport.response['data']['task_id'] = '12345'
        self.urls = tuple('https://ai-video-studio-poc-demo.s3.us-east-1.amazonaws.com/' + name + '?X-Amz-Signature=PRIVATE_SENTINEL'
                          for name in ('segment', 'first', 'second'))
        self.output_url = 'https://example.com/output?signature=OUTPUT_SECRET'
        self.downloader = fixtures.FakeDownloader(dict(zip(self.urls, (b'video1', b'neutral', b'smiling'))))
        self.validator = Mock(side_effect=lambda video, image, frames: {
            'video_sha256': digest(video), 'image_sha256': digest(image), 'duration_seconds': frames/24})
        hashes = tuple(digest(self.base / 'segments' / f'segment_{i:02d}.mp4') for i in range(1, 6))
        refs = tuple(digest(self.references / name) for name in ('test1.png', 'test2.jpeg'))
        for name, value in (('SEGMENT_HASHES', hashes), ('REFERENCE_HASHES', refs),
                            ('AUDIO_HASH', digest(self.base / 'segments' / 'original_continuous_audio.m4a'))):
            replacement = patch.object(full, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)

    def runner(self, enabled=False):
        return full.FullVideoRunner(self.base / 'kling-workflow', base=self.base, references=self.references,
                                    enabled=enabled, transport=self.transport, downloader=self.downloader,
                                    validator=self.validator)

    def submit(self, runner, segment=1):
        self.downloader.contents[self.urls[0]] = f'video{segment}'.encode()
        return runner.submit(segment, full.confirmation(segment), url_supplier=lambda: self.urls)

    def succeed(self):
        self.transport.response = {'code': 0, 'data': {'task_id': '12345', 'task_status': 'succeed',
            'task_result': {'videos': [{'url': self.output_url}]}}}

    def assert_no_sensitive_disk_data(self):
        for path in (self.base / 'kling-workflow').rglob('*'):
            if path.is_file() and path.suffix != '.mp4':
                content = path.read_bytes()
                for secret in (b'PRIVATE_SENTINEL', b'OUTPUT_SECRET', b'X-Amz', b'Authorization'):
                    self.assertNotIn(secret, content)

    def test_five_plans_have_unique_persistent_keys_and_expected_settings(self):
        runner = self.runner()
        jobs = [runner.plan(i) for i in range(1, 6)]
        self.assertEqual(len({job.idempotency_key for job in jobs}), 5)
        self.assertEqual(self.runner().plan(1).id, jobs[0].id)
        self.assertEqual([job.request.prompt for job in jobs], [EDIT_PROMPT]*5)
        self.assertEqual(self.validator.call_args_list[-3].args[2], 118)
        self.assertEqual(self.transport.posts + self.transport.gets + self.downloader.calls, [])
        self.assert_no_sensitive_disk_data()

    def test_payload_order_prompt_and_settings_match_test3(self):
        self.runner().plan(1)
        self.submit(self.runner(True))
        payload = self.transport.posts[0][1]
        self.assertEqual(payload['prompt'], EDIT_PROMPT)
        self.assertEqual(payload['image_list'], [{'image_url': url} for url in self.urls[1:]])
        self.assertEqual(payload['video_list'], [{'video_url': self.urls[0], 'refer_type': 'base', 'keep_original_sound': 'yes'}])
        self.assertEqual((payload['model_name'], payload['mode'], payload['sound'], payload['multi_shot']),
                         ('kling-v3-omni', 'pro', 'off', False))
        self.assertEqual([call[1] for call in self.downloader.calls], [full.VIDEO_LIMIT, full.IMAGE_LIMIT, full.IMAGE_LIMIT])
        self.assertIsNone(self.runner().provider.urls)
        self.assert_no_sensitive_disk_data()

    def test_live_flag_and_exact_per_segment_confirmation_required(self):
        self.runner().plan(1)
        supplier = Mock(return_value=self.urls)
        for enabled, authorization in ((False, full.confirmation(1)), (True, full.confirmation(2)), (True, '')):
            with self.assertRaises(SafetyError):
                self.runner(enabled).submit(1, authorization, url_supplier=supplier)
        supplier.assert_not_called()
        for operation in ('poll', 'download'):
            with self.assertRaises(SafetyError):
                getattr(self.runner(), operation)(1)
        self.assertEqual(self.transport.posts + self.transport.gets + self.downloader.calls, [])

    def test_fresh_urls_do_not_change_job_or_idempotency(self):
        job = self.runner().plan(1)
        old_urls = self.urls
        self.urls = tuple(url + 'new' for url in old_urls)
        self.downloader.contents.update({new: self.downloader.contents[old] for new,old in zip(self.urls,old_urls)})
        self.submit(self.runner(True))
        self.assertEqual(self.runner().job(1).id, job.id)
        self.assert_no_sensitive_disk_data()

    def test_all_media_hashes_verified_before_any_authorization(self):
        self.runner().plan(1)
        for url in self.urls:
            original = self.downloader.contents[url]
            self.downloader.contents[url] = b'incorrect'
            with self.subTest(url=url), self.assertRaises(SafetyError):
                self.submit(self.runner(True)) if url != self.urls[0] else self.runner(True).submit(
                    1, full.confirmation(1), url_supplier=lambda: self.urls)
            self.downloader.contents[url] = original
        self.assertEqual(self.transport.posts, [])
        self.assertEqual(self.runner().job(1).reserved_units, 0)

    def test_changed_local_photos_or_video_block_submission(self):
        self.runner().plan(1)
        for path in self.runner()._paths(1):
            before = path.read_bytes()
            path.write_bytes(b'changed')
            with self.assertRaises(SafetyError):
                self.submit(self.runner(True))
            path.write_bytes(before)
        self.assertEqual(self.downloader.calls + self.transport.posts, [])

    def test_invalid_bucket_or_insecure_urls_block_downloads(self):
        self.runner().plan(1)
        for url in ('http://ai-video-studio-poc-demo.s3.us-east-1.amazonaws.com/v',
                    'https://other.s3.us-east-1.amazonaws.com/v', 'https://example.com/v'):
            with self.assertRaises(SafetyError):
                self.runner(True).submit(1, full.confirmation(1), url_supplier=lambda: (url, *self.urls[1:]))
        self.assertEqual(self.downloader.calls, [])

    def test_timeout_keeps_reservation_and_never_resubmits_or_refetches(self):
        self.runner().plan(1)
        self.transport.error = TimeoutError('PRIVATE_SENTINEL')
        runner = self.runner(True)
        result = self.submit(runner)
        self.assertEqual((result.status, result.reserved_units), ('submission_unknown', 6000))
        supplier = Mock()
        self.runner(True).submit(1, full.confirmation(1), url_supplier=supplier)
        supplier.assert_not_called()
        self.assertEqual(len(self.transport.posts), 1)
        self.assertIsNone(runner.provider.urls)
        self.assert_no_sensitive_disk_data()

    def test_concurrent_same_job_has_one_post(self):
        self.runner().plan(1)
        runners = [self.runner(True), self.runner(True)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(self.submit, runners))
        self.assertEqual(len(self.transport.posts), 1)

    def test_shared_aggregate_cap_counts_five_reservations_and_rejects_sixth(self):
        runner = self.runner(True)
        for i in range(1, 6):
            job = runner.plan(i)
            runner.service.authorize(job.id)
        with closing(sqlite3.connect(runner.service.store.path)) as c:
            self.assertEqual(c.execute('SELECT SUM(reserved_units) FROM character_swap_jobs').fetchone()[0], 30000)
        extra = runner.service.create_job('offline-extra', 'kling', runner._request(1), 6000)
        with self.assertRaises(SafetyError):
            runner.service.authorize(extra.id)
        self.assertEqual(self.transport.posts, [])

    def test_aggregate_authorizations_are_atomic_under_concurrency(self):
        runner = self.runner(True)
        jobs = [runner.plan(i) for i in range(1, 6)]
        jobs.append(runner.service.create_job('extra-offline', 'kling', runner._request(1), 6000))
        def authorize(job):
            try:
                runner.service.authorize(job.id)
                return True
            except SafetyError:
                return False
        with ThreadPoolExecutor(max_workers=6) as pool:
            self.assertEqual(sum(pool.map(authorize, jobs)), 5)
        with closing(sqlite3.connect(runner.service.store.path)) as c:
            self.assertEqual(c.execute('SELECT SUM(reserved_units) FROM character_swap_jobs').fetchone()[0], 30000)

    def test_success_without_output_is_not_persisted_as_success(self):
        self.runner().plan(1)
        self.submit(self.runner(True))
        self.transport.response = {'code':0, 'data':{'task_id':'12345','task_status':'succeed'}}
        result = self.runner(True).poll(1)
        self.assertNotEqual(result.status, 'succeeded')
        self.assertEqual(result.reserved_units, 6000)
        self.submit(self.runner(True))
        self.assertEqual(len(self.transport.posts), 1)

    def test_real_transport_fake_https_keeps_key_and_signed_urls_off_disk(self):
        self.runner().plan(1)
        self.succeed()
        connections = fixtures.Connections(fixtures.Response(json.dumps(self.transport.response).encode()))
        transport = full.KlingHTTPTransport(lambda: 'DUMMY_API_KEY_SENTINEL', enabled=True,
                                             connection_factory=connections)
        runner = self.runner(True)
        runner.provider.transport = transport
        self.assertEqual(self.submit(runner).status, 'succeeded')
        self.assertEqual(len(connections.requests), 1)
        self.assertEqual(connections.requests[0][0], 'POST')
        self.assert_no_sensitive_disk_data()
        for path in runner.workspace.rglob('*'):
            if path.is_file():
                self.assertNotIn(b'DUMMY_API_KEY_SENTINEL', path.read_bytes())

    def test_injected_validation_failure_blocks_before_post(self):
        self.runner().plan(1)
        self.validator.side_effect = SafetyError('invalid media')
        with self.assertRaises(SafetyError):
            self.submit(self.runner(True))
        self.assertEqual(self.transport.posts + self.downloader.calls, [])

    def test_poll_and_download_never_persist_output_url(self):
        self.runner().plan(1)
        self.submit(self.runner(True))
        self.succeed()
        self.assertEqual(self.runner(True).poll(1).status, 'succeeded')
        self.assertEqual(self.runner().job(1).video_url, full.RESULT_MARKER)
        self.downloader.contents[self.output_url] = b'output video'
        runner = self.runner(True)
        output = runner.download(1)
        self.assertEqual(output.read_bytes(), b'output video')
        self.assertIsNone(runner.provider.output_url)
        with self.assertRaises(SafetyError):
            runner.download(1)
        self.assertEqual(len(self.transport.posts), 1)
        self.assert_no_sensitive_disk_data()

    def test_missing_output_or_failed_task_does_not_release_reservation(self):
        self.runner().plan(1)
        self.submit(self.runner(True))
        self.transport.response = {'code': 0, 'data': {'task_id': '12345', 'task_status': 'failed',
                                                     'task_status_msg': 'PRIVATE_SENTINEL'}}
        result = self.runner(True).poll(1)
        self.assertEqual((result.status, result.reserved_units), ('failed', 6000))
        self.submit(self.runner(True))
        self.assertEqual(len(self.transport.posts), 1)
        self.assert_no_sensitive_disk_data()

    def test_missing_task_id_or_unsuccessful_job_cannot_poll_or_download(self):
        self.runner().plan(1)
        with self.assertRaises(SafetyError):
            self.runner(True).poll(1)
        with self.assertRaises(SafetyError):
            self.runner(True).download(1)
        self.assertEqual(self.transport.gets, [])

    def test_tampered_persisted_settings_fail_closed(self):
        runner = self.runner()
        job = runner.plan(1)
        with closing(sqlite3.connect(runner.service.store.path)) as c:
            request = json.loads(c.execute('SELECT request_json FROM character_swap_jobs WHERE id=?',(job.id,)).fetchone()[0])
            request['prompt'] = 'changed'
            c.execute('UPDATE character_swap_jobs SET request_json=? WHERE id=?',(json.dumps(request),job.id))
            c.commit()
        with self.assertRaises(SafetyError):
            self.submit(self.runner(True))

    def test_workspace_and_continuous_audio_protection(self):
        with self.assertRaises(SafetyError):
            full.FullVideoRunner(self.base / 'test3', base=self.base)
        audio = self.base / 'segments' / 'original_continuous_audio.m4a'
        audio.write_bytes(b'changed')
        with self.assertRaises(SafetyError):
            self.runner().plan(1)

    def test_cli_plan_is_local_and_sanitizes_unexpected_errors(self):
        runner = self.runner()
        with patch.object(full, 'FullVideoRunner', return_value=runner), patch.object(full, 'masked_input') as secret, \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(full.main(['plan', '--segment', '01']), 0)
        secret.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())['estimated_api_units'], 6)
        with patch.object(full, 'FullVideoRunner', side_effect=ValueError('PRIVATE_SENTINEL')), \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(full.main(['plan', '--segment', '01']), 2)
        self.assertNotIn('PRIVATE_SENTINEL', output.getvalue())

    def test_cli_missing_ffprobe_reports_safe_actionable_error(self):
        runner = self.runner()
        runner.validator = full.validate_segment
        # Exercise the real validator's missing-inspector path using signature-valid fixtures.
        video, image, second = runner._paths(2)
        video.write_bytes(b'\x00\x00\x00\x18ftypisomoffline')
        image.write_bytes(b'\x89PNG\r\n\x1a\noffline')
        second.write_bytes(b'\xff\xd8\xffoffline')
        hashes = list(full.SEGMENT_HASHES)
        hashes[1] = digest(video)
        with patch.object(full, 'FullVideoRunner', return_value=runner), \
             patch.object(full, 'SEGMENT_HASHES', tuple(hashes)), \
             patch.object(full, 'REFERENCE_HASHES', (digest(image), digest(second))), \
             patch('character_swap.poc_media.shutil.which', return_value=None), \
             patch('character_swap.poc_media.subprocess.run') as process, \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(full.main(['plan','--segment','02']), 2)
        self.assertIn('ffprobe was not found', output.getvalue())
        self.assertIn('$env:PATH', output.getvalue())
        process.assert_not_called()
        with closing(sqlite3.connect(runner.service.store.path)) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM character_swap_jobs').fetchone()[0], 0)
        self.assertEqual(self.transport.posts + self.transport.gets + self.downloader.calls, [])


class SegmentValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.video = Path(self.temp.name) / 'segment.mp4'
        self.image = Path(self.temp.name) / 'test2.jpeg'
        self.video.write_bytes(b'\x00\x00\x00\x18ftypisomoffline')
        self.image.write_bytes(b'\xff\xd8\xffoffline')
        self.info = fixtures.video_metadata()
        self.info['streams'][0].update(width=1920, height=1080)
        self.picture = {'streams':[{'codec_name':'mjpeg','width':600,'height':900}]}
        self.frames = 120

    def validate(self):
        return full.validate_segment(self.video,self.image,self.frames,
            probe=lambda p: self.info if Path(p)==self.video else self.picture,
            decoder=lambda v,i:(self.frames,(1920,1080),(600,900)))

    def test_five_second_and_short_final_segment_validate_without_padding(self):
        self.validate()
        self.frames = 118
        self.info['streams'][0].update(nb_read_frames='118',duration=str(118/24))
        self.info['format']['duration'] = str(118/24)
        self.info['streams'][1]['duration'] = '4.908667'
        self.validate()

    def test_video_timing_fps_frames_and_audio_fail_closed(self):
        original = deepcopy(self.info)
        for index,key,value in ((0,'duration','4.9'),(0,'avg_frame_rate','30/1'),
                                (0,'nb_read_frames','119'),(0,'width',1280),
                                (0,'duration','NaN'),(0,'start_time','NaN'),
                                (1,'duration','4.8'),(1,'start_time','0.1'),(1,'codec_name','mp3')):
            self.info = deepcopy(original)
            self.info['streams'][index][key] = value
            with self.subTest(key=key), self.assertRaises(SafetyError):
                self.validate()

    def test_image_format_dimensions_ratio_and_size_fail_closed(self):
        for field,value in (('codec_name','png'),('width',299),('width',3000)):
            self.picture = {'streams':[{'codec_name':'mjpeg','width':600,'height':900}]}
            self.picture['streams'][0][field] = value
            with self.assertRaises(SafetyError):
                self.validate()
        self.picture = {'streams':[{'codec_name':'mjpeg','width':600,'height':900}]}
        with patch('character_swap.poc_media.IMAGE_LIMIT',1), self.assertRaises(SafetyError):
            self.validate()
