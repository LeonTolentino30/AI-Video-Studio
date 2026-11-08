"""Confirmed 4.0 serialization and mocked response failures; zero HTTP calls."""

from dataclasses import replace
import json
import unittest
from unittest.mock import Mock, patch

from character_swap.providers.base import CharacterEditRequest
from character_swap.providers.kling4 import (
    Kling4Provider, EDIT_PROMPT, VIDEO_ID, PRIMARY_ID, SECONDARY_ID,
    SUBMISSION_PATH, SUBMISSION_URL,
)
from character_swap.store import SafetyError


class Kling4SerializationTests(unittest.TestCase):
    def setUp(self):
        self.provider = Kling4Provider()
        self.urls = tuple('https://ai-video-studio-poc-demo.s3.us-east-1.amazonaws.com/' + name
                          + '?X-Amz-Signature=SIGNED_URL_SENTINEL' for name in ('video.mp4','test1.png','test2.jpeg'))
        self.request = CharacterEditRequest(self.urls[0],self.urls[1],EDIT_PROMPT,True,self.urls[2],True)

    def test_exact_confirmed_wire_shape(self):
        expected = {'contents':[
            {'type':'prompt','text':EDIT_PROMPT},
            {'type':'base_video','url':self.urls[0],'id':VIDEO_ID},
            {'type':'refer_image','url':self.urls[1],'id':PRIMARY_ID},
            {'type':'refer_image','url':self.urls[2],'id':SECONDARY_ID}],
            'settings':{'resolution':'1080p','aspect_ratio':'adaptive'}}
        self.assertEqual(self.provider.build_payload(self.request),expected)
        self.assertEqual(SUBMISSION_URL,'https://api-singapore.klingai.com/omni-video/kling-4.0')
        self.assertEqual(SUBMISSION_PATH,'/omni-video/kling-4.0')
        self.assertEqual(json.loads(json.dumps(expected)),expected)

    def test_custom_ids_are_unique_and_every_id_is_referenced(self):
        payload = self.provider.build_payload(self.request)
        assets = payload['contents'][1:]
        self.assertEqual(len({asset['id'] for asset in assets}),3)
        self.assertEqual(sum(asset['type']=='base_video' for asset in assets),1)
        for asset in assets:
            self.assertIn('@'+asset['id'],payload['contents'][0]['text'])

    def test_no_legacy_or_unverified_fields_are_serialized(self):
        payload = self.provider.build_payload(self.request)
        self.assertEqual(set(payload),{'contents','settings'})
        self.assertEqual(set(payload['settings']),{'resolution','aspect_ratio'})
        for key in ('model_name','image_list','video_list','refer_type','keep_original_sound',
                    'sound','mode','multi_shot','duration','audio','external_task_id'):
            self.assertNotIn('"'+key+'"',json.dumps(payload))

    def test_insecure_or_malformed_asset_urls_fail_without_echoing_values(self):
        for uri in ('http://example.com/SECRET','https://user:SECRET@example.com/a',
                    'file:///SECRET','https://example.com:444/SECRET',
                    'https://example.com/SECRET#fragment','sha256:SECRET'):
            for field in ('video_uri','reference_image_uri','second_reference_image_uri'):
                with self.subTest(uri=uri,field=field),self.assertRaises(SafetyError) as caught:
                    self.provider.build_payload(replace(self.request,**{field:uri}))
                self.assertNotIn('SECRET',str(caught.exception))

    def test_missing_reference_or_changed_instructions_are_rejected(self):
        for changes in ({'second_reference_image_uri':None},{'prompt':'changed'},
                        {'complete_prompt':False},{'preserve_audio':False}):
            with self.subTest(changes=changes),self.assertRaises(SafetyError):
                self.provider.build_payload(replace(self.request,**changes))

    def test_no_files_or_network_needed_and_runtime_urls_are_not_retained(self):
        with patch('socket.create_connection') as network,patch('builtins.open') as files:
            self.provider.build_payload(self.request)
        network.assert_not_called()
        files.assert_not_called()
        self.assertIsNone(self.provider.transport)
        self.assertIsNone(self.provider.last_report)
        self.assertIsNone(self.provider.take_protected_video_url())

    def test_fresh_signed_urls_do_not_change_prompt_settings_or_order(self):
        first = self.provider.build_payload(self.request)
        second = self.provider.build_payload(replace(self.request,
            video_uri=self.urls[0]+'renewed',reference_image_uri=self.urls[1]+'renewed',
            second_reference_image_uri=self.urls[2]+'renewed'))
        self.assertEqual(first['settings'],second['settings'])
        self.assertEqual(first['contents'][0],second['contents'][0])
        self.assertEqual([a['id'] for a in first['contents'][1:]],
                         [a['id'] for a in second['contents'][1:]])

    def test_live_calls_remain_blocked_even_for_a_valid_serializable_request(self):
        with patch('socket.create_connection') as network:
            self.provider.build_payload(self.request)
            with self.assertRaises(SafetyError):
                self.provider.submit(self.request)
            with self.assertRaises(SafetyError):
                self.provider.get_status('12345')
        network.assert_not_called()

    def test_mocked_receipts_errors_and_results_cannot_be_assumed_valid(self):
        fake_transport = Mock()
        for response in ({'code':0,'data':{'task_id':'123','task_status':'submitted'}},
                         {'code':0,'data':{'task_id':'123','task_status':'succeed',
                           'task_result':{'videos':[{'url':self.urls[0]}]}}},
                         {'code':401,'message':'API_KEY_SENTINEL '+self.urls[0]},
                         {'id':'123','status':'completed'},None):
            fake_transport.get.return_value = response
            with self.assertRaises(SafetyError) as caught:
                self.provider.parse_response(fake_transport.get())
            self.assertNotIn('SIGNED_URL_SENTINEL',str(caught.exception))
            self.assertNotIn('API_KEY_SENTINEL',str(caught.exception))
