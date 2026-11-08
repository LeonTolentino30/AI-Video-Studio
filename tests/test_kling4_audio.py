"""Local restoration safety tests plus a real offline FFmpeg round trip."""

from array import array
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from character_swap import kling4_audio as audio
from character_swap.poc_media import digest
from character_swap.store import SafetyError


def info(frames=600, audio_duration='25'):
    return {'format':{'duration':str(frames/24)},'streams':[
        {'codec_type':'video','codec_name':'h264','width':1920,'height':1080,
         'avg_frame_rate':'24/1','r_frame_rate':'24/1','nb_read_frames':str(frames),
         'duration':str(frames/24),'start_time':'0'},
        {'codec_type':'audio','codec_name':'aac','sample_rate':'48000','channels':2,
         'nb_read_frames':'1170','duration':audio_duration,'start_time':'0'}]}


class AudioRestorationSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source,self.generated,self.output = [self.folder/name for name in ('source.mp4','download.mp4','final.mp4')]
        self.source.write_bytes(b'original source')
        self.generated.write_bytes(b'generated video')
        self.source_info = info(audio_duration='24.916667')
        self.generated_info = info()
        self.final_info = info()
        self.commands = []
        def run(args):
            self.commands.append(args)
            if args[-1].endswith('verified.mp4'):
                Path(args[-1]).write_bytes(b'verified output')
            return b''
        def probe(path,tool):
            return deepcopy(self.source_info if path==self.source else self.generated_info if path==self.generated else self.final_info)
        pcm = MagicMock()
        pcm.__len__.return_value = 2_400_000
        patches = [patch.object(audio,'SOURCE_HASH',digest(self.source)),
                   patch.object(audio,'_tools',return_value=('ffmpeg','ffprobe')),
                   patch.object(audio,'_run',side_effect=run),patch.object(audio,'_probe',side_effect=probe),
                   patch.object(audio,'_frame_hashes',return_value=['#tb 0: 1/24'] +
                                [f'0, {i}, {i}, 1, 100, framehash' for i in range(600)]),
                   patch.object(audio,'_pcm',return_value=pcm),patch.object(audio,'_audio_match',return_value=1.0)]
        self.mocks = []
        for replacement in patches:
            self.mocks.append(replacement.start())
            self.addCleanup(replacement.stop)

    def restore(self,allow=True):
        return audio.restore_audio(self.generated,self.output,source=self.source,allow_tail_adjustment=allow)

    def test_streamcopy_original_audio_mapping_and_verified_separate_output(self):
        before = (digest(self.source),digest(self.generated))
        report = self.restore()
        self.assertEqual(report['frames'],600)
        self.assertEqual(report['duration_seconds'],25)
        self.assertEqual(report['audio_tail_action'],'pad_silence')
        conversion = self.commands[0]
        self.assertEqual(conversion[conversion.index('-c:v')+1],'copy')
        self.assertEqual(conversion[conversion.index('-c:a')+1],'aac')
        self.assertIn('1:a:0',conversion)
        self.assertNotIn('0:a:0',conversion)
        self.assertNotIn('-shortest',conversion)
        self.assertFalse(any('atempo' in item or 'setpts' in item and 'asetpts' not in item for item in conversion))
        self.assertEqual((digest(self.source),digest(self.generated)),before)
        self.assertTrue(self.output.exists())

    def test_small_tail_adjustment_requires_explicit_approval(self):
        with self.assertRaisesRegex(SafetyError,'allow-tail-adjustment'):
            self.restore(False)
        self.assertEqual(self.commands,[])
        self.assertFalse(self.output.exists())

    def test_excessive_duration_difference_always_rejected(self):
        self.source_info['streams'][1]['duration']='24'
        with self.assertRaisesRegex(SafetyError,'0.25'):
            self.restore()
        self.assertEqual(self.commands,[])

    def test_shorter_output_can_only_trim_audio_tail_not_speed(self):
        self.source_info['streams'][1]['duration']='25.1'
        report = self.restore()
        self.assertEqual(report['audio_tail_action'],'trim_tail')
        self.assertAlmostEqual(report['audio_tail_adjustment_seconds'],-0.1)

    def test_existing_output_and_input_paths_are_never_overwritten(self):
        self.output.write_bytes(b'existing')
        with self.assertRaises(SafetyError):
            self.restore()
        self.assertEqual(self.output.read_bytes(),b'existing')
        with self.assertRaises(SafetyError):
            audio.restore_audio(self.generated,self.source,source=self.source)

    def test_wrong_original_source_hash_is_rejected(self):
        self.source.write_bytes(b'wrong')
        with self.assertRaises(SafetyError):
            self.restore()
        self.assertEqual(self.commands,[])

    def test_wrong_fps_resolution_duration_or_frame_count_is_rejected(self):
        original = info()
        for key,value in (('avg_frame_rate','30/1'),('width',1280),('nb_read_frames','599'),
                          ('duration','24'),('start_time','0.1')):
            self.generated_info = deepcopy(original)
            self.generated_info['streams'][0][key]=value
            with self.subTest(key=key),self.assertRaises(SafetyError):
                self.restore()
        self.assertFalse(self.output.exists())

    def test_frame_verification_failure_publishes_no_final(self):
        self.mocks[4].side_effect=[['original']*600,['changed']*600]
        with self.assertRaises(SafetyError):
            self.restore()
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.folder.glob('.audio-restore-*')),[])

    def test_audio_verification_failure_publishes_no_final(self):
        self.mocks[6].side_effect=ValueError('mismatched audio')
        with self.assertRaises(SafetyError):
            self.restore()
        self.assertFalse(self.output.exists())

    def test_zero_start_and_duration_validation_for_final_audio(self):
        self.final_info['streams'][1]['start_time']='0.02'
        with self.assertRaises(SafetyError):
            self.restore()
        self.assertFalse(self.output.exists())

    def test_atomic_publish_does_not_replace_concurrently_created_output(self):
        def conflict(source,destination):
            Path(destination).write_bytes(b'other process')
            raise FileExistsError
        with patch.object(audio.os,'link',side_effect=conflict),self.assertRaises(SafetyError):
            self.restore()
        self.assertEqual(self.output.read_bytes(),b'other process')


class AudioComparisonTests(unittest.TestCase):
    def test_irregular_frame_timing_is_rejected(self):
        with self.assertRaises(ValueError):
            audio._validate_frame_timeline(['#tb 0: 1/24','0, 0, 0, 1, 100, hash',
                                            '0, 2, 2, 1, 100, hash'],2)

    def test_matching_audio_and_silence(self):
        self.assertEqual(audio._audio_match(array('h',[1,-1]*100),array('h',[1,-1]*100)),1)
        self.assertEqual(audio._audio_match(array('h',[0]*100),array('h',[0]*100)),1)

    def test_shifted_or_unrelated_audio_is_rejected(self):
        for result in (array('h',[-1,1]*100),array('h',[0]*200),array('h',[1])):
            with self.assertRaises(ValueError):
                audio._audio_match(array('h',[1,-1]*100),result)


class RealOfflineFFmpegRestorationTests(unittest.TestCase):
    def test_actual_25_second_streamcopy_and_original_audio_restoration(self):
        portable = Path(r'C:\Users\leont\Tools\ffmpeg-kling-poc\extracted\ffmpeg-9.0.2-essentials_build\bin')
        try:
            ffmpeg,ffprobe = audio._tools(portable if portable.is_dir() else None)
        except SafetyError:
            self.skipTest('Existing local FFmpeg tools unavailable; no downloads allowed')
        with TemporaryDirectory() as folder:
            folder = Path(folder)
            source,generated,output = [folder/name for name in ('original.m4a','generated.mp4','final.mp4')]
            def run(args):
                subprocess.run(args,capture_output=True,check=True,timeout=120)
            run([ffmpeg,'-nostdin','-v','error','-n','-f','lavfi','-i',
                 'sine=frequency=440:sample_rate=48000:duration=24.916667','-ac','2','-c:a','aac','-b:a','192k',str(source)])
            run([ffmpeg,'-nostdin','-v','error','-n','-f','lavfi','-i',
                 'color=c=blue:size=1920x1080:rate=24','-frames:v','600','-c:v','libx264',
                 '-preset','ultrafast','-crf','28','-pix_fmt','yuv420p',str(generated)])
            before=(digest(source),digest(generated))
            with patch.object(audio,'SOURCE_HASH',before[0]):
                report = audio.restore_audio(generated,output,source=source,tools_bin=Path(ffmpeg).parent,
                                             allow_tail_adjustment=True)
            self.assertEqual((report['frames'],report['duration_seconds']),(600,25))
            self.assertTrue(report['decoded_frames_and_timing_identical'])
            self.assertGreater(report['original_audio_correlation'],0.99)
            self.assertEqual((digest(source),digest(generated)),before)
            self.assertTrue(output.is_file())
