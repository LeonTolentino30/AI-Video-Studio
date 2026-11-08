"""Kling 4.0 offline validation/planning; no speculative networking."""

from contextlib import closing, redirect_stdout
from copy import deepcopy
from dataclasses import asdict
import io
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from character_swap import kling4_plan as plan
from character_swap.providers import kling4
from character_swap.kling_poc_test3 import EDIT_PROMPT as ORIGINAL_PROMPT
from character_swap.poc_media import digest
from character_swap.providers.base import CharacterEditRequest
from character_swap.store import SafetyError
import test_kling_poc as fixtures


class Kling4IntentTests(unittest.TestCase):
    def test_prompt_changes_only_reference_syntax(self):
        recovered = (kling4.EDIT_PROMPT.replace('@original_video','<<<video_1>>>')
                     .replace('@primary_person','<<<image_1>>>')
                     .replace('@additional_identity','<<<image_2>>>'))
        self.assertEqual(recovered, ORIGINAL_PROMPT)
        intent = kling4.EditIntent()
        self.assertEqual(intent.endpoint, '/omni-video/kling-4.0')
        self.assertEqual([role for role,_ in intent.input_roles], ['base_video','refer_image','refer_image'])
        self.assertEqual((intent.requested_resolution,intent.requested_aspect_ratio),('1080p','adaptive'))
        self.assertTrue(intent.preserve_original_audio)

    def test_provider_and_payload_are_disabled_without_a_transport(self):
        provider = kling4.Kling4Provider()
        request = CharacterEditRequest('sha256:video','sha256:photo',kling4.EDIT_PROMPT)
        for operation,args in ((provider.submit,(request,)),(provider.build_payload,(request,)),
                               (provider.get_status,('12345',))):
            with self.assertRaises(SafetyError):
                operation(*args)
        self.assertIsNone(provider.transport)
        self.assertNotIn('keep_original_sound',asdict(kling4.EditIntent()))

    def test_confirmed_video_limit_boundaries(self):
        valid = dict(size=300_000_000,width=3840,height=2160,duration=30,fps=60,suffix='.mov')
        plan.validate_video_limits(**valid)
        plan.validate_video_limits(size=1,width=700,height=700,duration=3,fps=24,suffix='.mp4')
        for field,value in (('size',300_000_001),('width',4554),('height',699),('duration',30.01),
                            ('duration',2.99),('fps',23),('fps',61),('duration','NaN'),('suffix','.avi')):
            data = dict(valid)
            data[field] = value
            with self.subTest(field=field),self.assertRaises(SafetyError):
                plan.validate_video_limits(**data)
        with self.assertRaises(SafetyError):
            plan.validate_video_limits(size=1,width=3000,height=3000,duration=25,fps=24,suffix='.mp4')


class Kling4MediaAndLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.video = self.folder/'Video_Project_5_24fps.mp4'
        self.images = (self.folder/'test1.png',self.folder/'test2.jpeg')
        for p,content in ((self.video,b'\x00\x00\x00\x18ftypisomoffline'),
                          (self.images[0],b'\x89PNG\r\n\x1a\noffline'),
                          (self.images[1],b'\xff\xd8\xffoffline')):
            p.write_bytes(content)
        for name,value in (('VIDEO_HASH',digest(self.video)),
                           ('REFERENCE_HASHES',tuple(digest(p) for p in self.images))):
            replacement = patch.object(plan,name,value)
            replacement.start()
            self.addCleanup(replacement.stop)
        self.info = fixtures.video_metadata()
        self.info['format']['duration'] = '24.916667'
        self.info['streams'][0].update(width=1920,height=1080,nb_read_frames='598',duration='24.916667')
        self.info['streams'][1].update(duration='24.908667',sample_rate='48000',channels=2)
        self.decoder = Mock(return_value=(598,(1920,1080),(600,900)))
        self.workspace = self.folder/'kling4-full-video'

    def probe(self,path):
        if Path(path)==self.video:
            return deepcopy(self.info)
        return {'streams':[{'codec_name':'png' if Path(path).suffix=='.png' else 'mjpeg','width':600,'height':900}]}

    def validate(self,folder=None):
        return plan.validate_inputs(self.folder,probe=self.probe,decoder=self.decoder)

    def planner(self):
        return plan.Kling4Planner(self.workspace,inputs=self.folder,validator=self.validate)

    def test_complete_media_validation_preserves_files_and_order(self):
        before = [digest(p) for p in (self.video,*self.images)]
        metadata = self.validate()
        self.assertEqual(metadata['frames'],598)
        self.assertEqual(metadata['reference_sha256'],before[1:])
        self.assertEqual(before,[digest(p) for p in (self.video,*self.images)])
        self.assertEqual(self.decoder.call_count,2)

    def test_altered_media_fails_before_planning(self):
        self.images[1].write_bytes(b'wrong')
        with self.assertRaises(SafetyError):
            self.planner().plan()
        self.assertFalse(self.workspace.exists())

    def test_timelines_audio_dimensions_and_decode_are_verified(self):
        original = deepcopy(self.info)
        for index,key,value in ((0,'nb_read_frames','597'),(0,'r_frame_rate','30/1'),
                                (0,'duration','25'),(0,'width',1280),(1,'duration','20'),
                                (1,'codec_name','mp3'),(1,'start_time','0.2')):
            self.info = deepcopy(original)
            self.info['streams'][index][key] = value
            with self.subTest(key=key),self.assertRaises(SafetyError):
                self.validate()
        self.info = original
        self.decoder.return_value = (597,(1920,1080),(600,900))
        with self.assertRaises(SafetyError):
            self.validate()

    def test_missing_ffprobe_fails_closed(self):
        with patch('character_swap.poc_media.shutil.which',return_value=None):
            with self.assertRaises(SafetyError):
                plan.validate_inputs(self.folder)

    def test_plans_are_persistent_idempotent_and_disabled_by_default(self):
        planner = self.planner()
        first,metadata = planner.plan()
        second,_ = self.planner().plan()
        self.assertEqual(first.id,second.id)
        self.assertEqual(first.status,'created')
        self.assertEqual(first.reserved_units,0)
        self.assertEqual(first.request.prompt,kling4.EDIT_PROMPT)
        self.assertEqual(first.model,'kling-4.0')
        for action in (planner.service.authorize,planner.service.submit,planner.service.poll):
            with self.assertRaises(SafetyError):
                action(first.id)
        with closing(sqlite3.connect(planner.service.store.path)) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM character_swap_jobs').fetchone()[0],1)
        self.assertNotIn(b'https:',Path(planner.service.store.path).read_bytes())
        self.assertNotIn(b'Authorization',Path(planner.service.store.path).read_bytes())

    def test_other_experiment_workspaces_are_rejected(self):
        for name in ('kling-omni-poc','kling-omni-poc-test2','kling-omni-poc-test3','kling-workflow'):
            with self.assertRaises(SafetyError):
                plan.Kling4Planner(self.folder/name)

    def test_cli_plan_reports_unverified_requirements_and_no_live_switch(self):
        with patch.object(plan,'Kling4Planner',return_value=self.planner()),redirect_stdout(io.StringIO()) as output:
            self.assertEqual(plan.main(['plan']),0)
        report = json.loads(output.getvalue())
        self.assertFalse(report['live_submission_enabled'])
        self.assertEqual(report['provisional_estimated_api_units'],45)
        self.assertEqual(report['unverified_requirements'],list(kling4.BLOCKERS))
        with redirect_stdout(io.StringIO()),patch('sys.stderr',new_callable=io.StringIO):
            with self.assertRaises(SystemExit):
                plan.main(['plan','--live'])

    def test_cli_never_reflects_arbitrary_exception_secrets(self):
        with patch.object(plan,'Kling4Planner',side_effect=ValueError('SECRET_URL_OR_KEY')),redirect_stdout(io.StringIO()) as output:
            self.assertEqual(plan.main(['plan']),2)
        self.assertNotIn('SECRET_URL_OR_KEY',output.getvalue())
