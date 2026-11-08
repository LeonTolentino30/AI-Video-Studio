"""Offline Kling 4.0 receipt, query, billing, transport and ledger tests."""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from character_swap.kling4_http import Kling4TaskHTTP, API_HOST
from character_swap.kling4_results import RESULT_MARKER
from character_swap.providers.base import CharacterEditRequest, TaskStatus
from character_swap.providers.kling4 import Kling4Provider, EDIT_PROMPT
from character_swap.service import CharacterSwapService, SpendingPolicy
from character_swap.store import JobStore, SafetyError
import test_kling_poc as fixtures


URL = 'https://example.com/result?signature=PROTECTED_URL_SENTINEL'


def task(status='processing'):
    data = {'id':'12345', 'status':status, 'create_time':1781080778802, 'update_time':1781080794151}
    if status == 'succeeded':
        data['outputs'] = [
            {'type':'image','url':'https://example.com/not-the-video'},
            {'type':'video','id':'video-1','url':URL,'watermark_url':URL+'&watermark', 'duration':'24.916667'},
            {'type':'audio','mp3_url':'https://example.com/not-the-video-either'}]
    return data


class Kling4ResponseTests(unittest.TestCase):
    def setUp(self):
        self.transport = Mock()
        self.provider = Kling4Provider(self.transport)

    def poll(self, data):
        self.transport.get.return_value = {'code':0,'data':[data]}
        return self.provider.get_status('12345')

    def test_creation_receipts_normalize_documented_fields(self):
        for status,normalized in (('submitted',TaskStatus.PENDING),('processing',TaskStatus.RUNNING),
                                  ('succeeded',TaskStatus.SUCCEEDED),('failed',TaskStatus.FAILED)):
            with self.subTest(status=status):
                result = self.provider.parse_response({'code':0,'data':task(status)})
                self.assertEqual((result.task_id,result.status),('12345',normalized))
        self.transport.assert_not_called()

    def test_polling_matches_requested_id_not_array_position(self):
        self.transport.get.return_value = {'code':0,'data':[{'id':'999','status':'submitted'},task()]}
        result = self.provider.get_status('12345')
        self.assertEqual(result.status,TaskStatus.RUNNING)
        self.transport.get.assert_called_once_with('/tasks?task_ids=12345')
        self.transport.post.assert_not_called()

    def test_missing_usage_and_billing_remain_unknown(self):
        for status in ('submitted','processing','succeeded','failed'):
            self.poll(task(status))
            report = self.provider.last_report.to_dict()
            self.assertIsNone(report['billing'])
            self.assertFalse(report['usage_available'])
            self.assertIsNone(report['billed_output_duration_seconds'])

    def test_video_extraction_discards_other_outputs_and_urls(self):
        result = self.poll(task('succeeded'))
        self.assertEqual(result.video_url,RESULT_MARKER)
        report = self.provider.last_report.to_dict()
        self.assertEqual(report['video_id'],'video-1')
        self.assertEqual(report['reported_video_duration_seconds'],'24.916667')
        self.assertEqual(report['protected_results_retention_days'],30)
        self.assertNotIn('PROTECTED_URL_SENTINEL',json.dumps(report))
        self.assertNotIn('PROTECTED_URL_SENTINEL',repr(self.provider.last_report))
        self.assertEqual(self.provider.take_protected_video_url(),URL)
        self.assertIsNone(self.provider.take_protected_video_url())

    def test_reported_and_billed_durations_are_separate_decimal_strings(self):
        data = task('succeeded')
        data['usage'] = {'input_duration':'24.92','output_duration':'25'}
        self.poll(data)
        report = self.provider.last_report.to_dict()
        self.assertEqual(report['normalized_input_duration_seconds'],'24.92')
        self.assertEqual(report['billed_output_duration_seconds'],'25')
        self.assertEqual(report['reported_video_duration_seconds'],'24.916667')

    def test_billing_cash_and_units_stay_separate(self):
        data = task('succeeded')
        data['billing'] = [
            {'charge_type':'unit','amount':'45.000','package_type':'video'},
            {'charge_type':'cash','amount':'1.23','currency':'USD','cash_type':'balance','list_price':'1.5'},
            {'charge_type':'cash','amount':'2.34','currency':'CNY','cash_type':'reward_balance'}]
        self.poll(data)
        self.assertEqual(self.provider.last_report.to_dict()['billing'],[
            {'charge_type':'unit','amount':'45.000','package_type':'video','currency':None,'cash_type':None,'list_price':None},
            {'charge_type':'cash','amount':'1.23','currency':'USD','cash_type':'balance','package_type':None,'list_price':'1.5'},
            {'charge_type':'cash','amount':'2.34','currency':'CNY','cash_type':'reward_balance','package_type':None,'list_price':None}])

    def test_empty_billing_is_distinct_from_absent_billing(self):
        data = task()
        data['billing'] = []
        data['usage'] = {'output_duration':'25'}
        self.poll(data)
        self.assertEqual(self.provider.last_report.to_dict()['billing'],[])
        self.assertIsNone(self.provider.last_report.usage.input_duration)

    def test_failure_details_are_sanitized(self):
        data = task('failed')
        data['message'] = 'API_KEY_SENTINEL '+URL
        result = self.poll(data)
        self.assertEqual(result.error.code,'task_failed')
        report = self.provider.last_report.to_dict()
        self.assertTrue(report['provider_failure_message_present'])
        self.assertNotIn('API_KEY_SENTINEL',json.dumps(report))
        self.assertNotIn('PROTECTED_URL_SENTINEL',json.dumps(report))

    def test_success_without_video_or_valid_url_is_not_success(self):
        for outputs in ([],[{'type':'image','url':URL}],
                        [{'type':'video','id':'v','duration':'25','url':None}],
                        [{'type':'video','id':'v','duration':'25','url':'   '}],
                        [{'type':'video','id':'v','duration':'25','url':'http://example.com/private'}]):
            data = task('succeeded')
            data['outputs'] = outputs
            with self.subTest(outputs=outputs):
                result = self.poll(data)
                self.assertEqual(result.status,TaskStatus.FAILED)
                self.assertEqual(result.error.code,'invalid_output_url')
                self.assertIsNone(self.provider.take_protected_video_url())

    def test_malformed_envelopes_and_task_matches_fail_closed(self):
        variants = (None,[],{}, {'code':True,'data':[task()]}, {'code':401,'message':URL},
                    {'code':0,'data':task()}, {'code':0,'data':[]}, {'code':0,'data':[None]},
                    {'code':0,'data':[{'id':'999','status':'processing'}]},
                    {'code':0,'data':[task(),task()]})
        for response in variants:
            self.transport.get.return_value = response
            with self.subTest(response=response),self.assertRaises(SafetyError) as error:
                self.provider.get_status('12345')
            self.assertNotIn('PROTECTED_URL_SENTINEL',str(error.exception))
            self.assertIsNone(self.provider.last_report)
            self.assertIsNone(self.provider.take_protected_video_url())

    def test_malformed_fields_fail_without_accepting_partial_report(self):
        variants = []
        for field,value in (('status','succeed'),('status',None),('message',{}),('outputs',{}),
                            ('outputs',[{}]),('usage',None),('billing',{}),('create_time',True),
                            ('update_time',0),('usage',{'output_duration':'NaN'})):
            data = task()
            data[field] = value
            variants.append(data)
        data = task('succeeded')
        data['outputs'].append(deepcopy(data['outputs'][1]))
        variants.append(data)
        for data in variants:
            with self.subTest(data=data),self.assertRaises(SafetyError):
                self.poll(data)

    def test_malformed_billing_and_usage_are_rejected(self):
        rows = [None,{}, {'charge_type':'unit','amount':'NaN','package_type':'video'},
                {'charge_type':'unit','amount':45,'package_type':'video'},
                {'charge_type':'cash','amount':'-1','currency':'USD'},
                {'charge_type':'cash','amount':'1','currency':'EUR'},
                {'charge_type':'cash','amount':'1','currency':'USD','cash_type':'SECRET'},
                {'charge_type':'unit','amount':'1','package_type':'unknown'},
                {'charge_type':'unit','amount':'1','package_type':'video','currency':'USD'}]
        for row in rows:
            data = task()
            data['billing'] = [row]
            with self.subTest(row=row),self.assertRaises(SafetyError):
                self.poll(data)

    def test_old_urls_are_cleared_before_failed_poll(self):
        self.poll(task('succeeded'))
        self.transport.get.side_effect = TimeoutError(URL)
        with self.assertRaises(SafetyError):
            self.provider.get_status('12345')
        self.assertIsNone(self.provider.take_protected_video_url())
        self.assertIsNone(self.provider.last_report)

    def test_invalid_task_id_does_not_call_transport(self):
        for task_id in ('123&task_ids=other','https://example.com/',None,''):
            with self.assertRaises(SafetyError):
                self.provider.get_status(task_id)
        self.transport.get.assert_not_called()

    def test_creation_response_missing_id_or_status_is_rejected(self):
        for data in ({},{'id':'12345'},{'status':'submitted'},{'task_id':'12345','task_status':'submitted'}):
            with self.assertRaises(SafetyError):
                self.provider.parse_response({'code':0,'data':data})

    def test_live_submission_remains_blocked_even_with_transport(self):
        with self.assertRaises(SafetyError):
            self.provider.submit(CharacterEditRequest('sha256:v','sha256:i',EDIT_PROMPT))
        self.transport.post.assert_not_called()


class Kling4TaskTransportTests(unittest.TestCase):
    def test_default_offline_does_not_read_key(self):
        key,connections = Mock(),fixtures.Connections()
        transport = Kling4TaskHTTP(key,connection_factory=connections)
        with self.assertRaises(SafetyError):
            transport.get('/tasks?task_ids=12345')
        key.assert_not_called()
        self.assertEqual(connections.requests,[])
        self.assertFalse(hasattr(transport,'post'))

    def test_fixed_host_get_verified_tls_auth_and_timeout(self):
        connections = fixtures.Connections(fixtures.Response(b'{"code":0,"data":[]}'))
        transport = Kling4TaskHTTP(lambda:'DUMMY_KEY',enabled=True,connection_factory=connections)
        transport.get('/tasks?task_ids=12345')
        self.assertEqual(connections.opened[0][0],API_HOST)
        options = connections.opened[0][1]
        self.assertEqual(options['timeout'],30)
        self.assertTrue(options['context'].check_hostname)
        method,path,body,headers = connections.requests[0]
        self.assertEqual((method,path,body),('GET','/tasks?task_ids=12345',None))
        self.assertEqual(headers['Authorization'],'Bearer DUMMY_KEY')
        self.assertEqual(connections.closed,1)

    def test_redirects_errors_timeouts_and_invalid_json_never_retry(self):
        variants = [fixtures.Connections(fixtures.Response(status=302)),
                    fixtures.Connections(fixtures.Response(status=401)),
                    fixtures.Connections(fixtures.Response(b'not-json '+URL.encode())),
                    fixtures.Connections(error=TimeoutError(URL)),
                    fixtures.Connections(fixtures.Response(b'x'*1_048_577))]
        for connections in variants:
            transport = Kling4TaskHTTP(lambda:'DUMMY_KEY',enabled=True,connection_factory=connections)
            with self.assertRaises(SafetyError) as error:
                transport.get('/tasks?task_ids=12345')
            self.assertEqual(len(connections.requests),1)
            self.assertNotIn('DUMMY_KEY',str(error.exception))
            self.assertNotIn('PROTECTED_URL_SENTINEL',str(error.exception))

    def test_unsupported_urls_paths_and_query_injection_rejected_before_auth(self):
        key,connections = Mock(),fixtures.Connections()
        transport = Kling4TaskHTTP(key,enabled=True,connection_factory=connections)
        for path in ('https://example.com/tasks','/omni-video/kling-4.0','/tasks?task_ids=123&extra=x',
                     '/tasks?task_ids=123,456','/tasks?task_ids='):
            with self.assertRaises(SafetyError):
                transport.get(path)
        key.assert_not_called()
        self.assertEqual(connections.opened,[])


class Kling4PersistenceTests(unittest.TestCase):
    def test_poll_persists_no_protected_urls_and_keeps_reservation(self):
        with TemporaryDirectory() as folder:
            transport = Mock()
            provider = Kling4Provider(transport)
            service = CharacterSwapService(JobStore(Path(folder)/'jobs.db'),{'kling4':provider},
                {'kling4':'kling-4.0'},SpendingPolicy(False,True,45000,45000))
            job = service.create_job('isolated-offline-key','kling4',
                CharacterEditRequest('sha256:video','sha256:image',EDIT_PROMPT),45000)
            job = service.authorize(job.id)
            job = service.store.update(job,status='pending',provider_task_id='12345')
            data = task('succeeded')
            data['usage'] = {'input_duration':'24.92','output_duration':'25'}
            data['billing'] = [{'charge_type':'unit','amount':'45','package_type':'video'}]
            transport.get.return_value = {'code':0,'data':[data]}
            result = service.poll(job.id)
            self.assertEqual(result.status,'succeeded')
            self.assertEqual(result.reserved_units,45000)
            self.assertEqual(result.video_url,RESULT_MARKER)
            self.assertNotIn(b'PROTECTED_URL_SENTINEL',Path(service.store.path).read_bytes())
            self.assertEqual(service.create_job('isolated-offline-key','kling4',job.request,45000).id,job.id)
            service.submit(job.id)
            transport.post.assert_not_called()

    def test_missing_video_preserves_reservation_without_resubmission(self):
        with TemporaryDirectory() as folder:
            transport = Mock()
            provider = Kling4Provider(transport)
            service = CharacterSwapService(JobStore(Path(folder)/'jobs.db'),{'kling4':provider},
                {'kling4':'kling-4.0'},SpendingPolicy(False,True,45000,45000))
            job = service.create_job('offline-missing-output','kling4',
                CharacterEditRequest('sha256:video','sha256:image',EDIT_PROMPT),45000)
            job = service.authorize(job.id)
            job = service.store.update(job,status='running',provider_task_id='12345')
            transport.get.return_value = {'code':0,'data':[{'id':'12345','status':'succeeded'}]}
            result = service.poll(job.id)
            self.assertEqual((result.status,result.reserved_units),('failed',45000))
            service.submit(job.id)
            transport.post.assert_not_called()
