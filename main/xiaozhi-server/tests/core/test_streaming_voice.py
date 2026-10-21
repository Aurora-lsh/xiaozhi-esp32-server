"""Incremental audio, cancellation, and bounded first-sentence regressions."""
import ast
import asyncio
import json
from pathlib import Path
import queue
import struct
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from core.providers.tts.dto.dto import SentenceType
from core.providers.tts.wav_stream import WavStream


def wav_header(rate=32000, channels=1, bits=16):
    fmt = struct.pack('<HHIIHH', 1, channels, rate, rate * channels * bits // 8, channels * bits // 8, bits)
    return b'RIFF' + struct.pack('<I', 36) + b'WAVEfmt ' + struct.pack('<I', 16) + fmt + b'data\0\0\0\0'


class WavTests(unittest.TestCase):
    def test_any_network_chunk_boundary_keeps_pcm_exactly(self):
        pcm = bytes(range(256)) * 8
        stream = wav_header() + pcm
        for size in (1, 3, 7, 44, 65, 4096):
            parser = WavStream()
            result = b''.join(parser.feed(stream[i:i+size]) for i in range(0, len(stream), size))
            parser.finish()
            self.assertEqual(pcm, result)
            self.assertEqual(32000, parser.sample_rate)

    def test_padded_metadata_is_skipped_before_audio(self):
        header = wav_header()
        stream = header[:12] + b'JUNK\x03\0\0\0abc\0' + header[12:] + b'\x01\0\x02\0'
        parser = WavStream()
        self.assertEqual(b'\x01\0\x02\0', parser.feed(stream))
        parser.finish()

    def test_unsupported_format_and_unbounded_header_fail_before_audio(self):
        for stream in (b'HTTP error instead of WAV', wav_header(channels=2), wav_header(bits=8),
                       wav_header()[:12] + b'JUNK' + struct.pack('<I', 1000000)):
            with self.subTest(stream=stream[:12]), self.assertRaises(ValueError):
                WavStream().feed(stream)

    def test_empty_and_truncated_pcm_are_rejected(self):
        for stream in (b'RIFF', wav_header(), wav_header() + b'\x01'):
            parser = WavStream()
            parser.feed(stream)
            with self.assertRaises(ValueError):
                parser.finish()


class FakeBase:
    def __init__(self, config, delete_audio_file):
        self.delete_audio_file = delete_audio_file
        self.tts_timeout = 5
        self.tts_audio_queue = queue.Queue()
        self._correct_words_pattern = None
        self.fallback = Mock()
        self.opus_encoder = SimpleNamespace(reset_state=Mock())
        self.conn = SimpleNamespace(sentence_id='turn', client_abort=False, stop_event=threading.Event(), sample_rate=16000)
        self.tts_text_buff, self.processed_chars, self.is_first_sentence = [], 0, True
        self.first_sentence_punctuations = ('，', ',', '。', '！', '？')
        self.punctuations = ('。', '！', '？')

    def to_tts_stream(self, text, handler):
        self.fallback(text)

    async def close(self):
        pass


class FakeOpus:
    def __init__(self, source_rate, target_rate, encoder, callback):
        self.callback = callback

    def feed(self, pcm=b'', last=False):
        if pcm:
            self.callback(b'opus-frame')


def tts_class():
    source = Path(__file__).resolve().parents[2] / 'core/providers/tts/gpt_sovits_v2.py'
    node = next(n for n in ast.parse(source.read_text(encoding='utf-8')).body if isinstance(n, ast.ClassDef))
    scope = dict(TTSProviderBase=FakeBase, logger=Mock(), TAG='test', asyncio=asyncio, threading=threading,
        aiohttp=aiohttp, WavStream=WavStream, StreamingOpus=FakeOpus, SentenceType=SentenceType,
        MarkdownCleaner=SimpleNamespace(clean_markdown=lambda text:text), parse_string_to_list=lambda value:[])
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), scope)
    return scope['TTSProvider']


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.release, self.first = asyncio.Event(), asyncio.Event()
        self.mode, self.received = 'ok', None
        async def tts(request):
            self.received = await request.json()
            if self.mode == 'bad':
                return web.Response(body=b'not a wave: upstream error')
            response = web.StreamResponse(headers={'Content-Type':'audio/wav'})
            await response.prepare(request)
            await response.write(wav_header() + b'\x01\0' * 2000)
            self.first.set()
            await self.release.wait()
            if self.mode == 'broken':
                request.transport.close()
                return response
            try:
                await response.write(b'\x02\0' * 1000)
                await response.write_eof()
            except (ConnectionError, RuntimeError):
                pass
            return response
        app = web.Application()
        app.router.add_post('/tts', tts)
        self.server = TestServer(app)
        await self.server.start_server()
        self.provider = tts_class()({'url':str(self.server.make_url('/tts')), 'streaming_mode':2}, True)
        self.provider.current_sentence_id = 'turn'

    async def asyncTearDown(self):
        self.release.set()
        await self.server.close()

    async def test_audio_arrives_before_http_response_completes(self):
        worker = asyncio.create_task(asyncio.to_thread(self.provider.to_tts_stream, '流式测试。'))
        await asyncio.wait_for(self.first.wait(), 2)
        item = await asyncio.wait_for(asyncio.to_thread(self.provider.tts_audio_queue.get, True, 1), 2)
        self.assertEqual(SentenceType.FIRST, item[0])
        self.assertFalse(worker.done())
        self.assertEqual(2, self.received['streaming_mode'])
        self.release.set()
        await asyncio.wait_for(worker, 2)
        self.provider.fallback.assert_not_called()

    async def test_abort_cancels_blocked_reader_without_waiting_for_server(self):
        worker = asyncio.create_task(asyncio.to_thread(self.provider.to_tts_stream, '取消测试。'))
        await asyncio.wait_for(self.first.wait(), 2)
        await asyncio.sleep(0.02)
        self.provider.conn.client_abort = True
        self.provider.conn.sentence_id = 'new-turn'
        self.provider.cancel_synthesis()
        await asyncio.wait_for(worker, 1)
        self.provider.opus_encoder.reset_state.assert_called_once()
        self.provider.fallback.assert_not_called()

    async def test_failure_before_first_audio_falls_back_once(self):
        self.mode = 'bad'
        await asyncio.to_thread(self.provider.to_tts_stream, '回退测试。')
        self.provider.fallback.assert_called_once()

    async def test_failure_after_audio_never_replays_the_sentence(self):
        self.mode = 'broken'
        worker = asyncio.create_task(asyncio.to_thread(self.provider.to_tts_stream, '不重复播放。'))
        await asyncio.wait_for(self.first.wait(), 2)
        await asyncio.sleep(0.02)
        self.release.set()
        await asyncio.wait_for(worker, 2)
        self.provider.fallback.assert_not_called()
        self.provider.opus_encoder.reset_state.assert_called_once()


class SegmentTests(unittest.TestCase):
    def test_punctuation_only_tail_never_reaches_tts_or_fallback(self):
        for mode in (0, 2):
            for text in ('？', '？！……', '   ', '～，。'):
                with self.subTest(mode=mode, text=text):
                    provider = tts_class()({'streaming_mode': mode}, True)
                    provider.to_tts_stream(text)
                    provider.fallback.assert_not_called()
                    self.assertTrue(provider.tts_audio_queue.empty())

    def provider(self):
        return tts_class()({'streaming_mode':2, 'first_sentence_max_chars':16}, True)

    def test_first_segment_uses_earliest_sufficient_pause_and_keeps_remainder(self):
        provider = self.provider()
        provider.tts_text_buff = ['你好呀，我和派大星在窗边，发现了一只风车。后来它飞走了！']
        self.assertEqual('你好呀，我和派大星在窗边', provider._get_segment_text())
        self.assertEqual('发现了一只风车', provider._get_segment_text())
        self.assertEqual('后来它飞走了', provider._get_segment_text())
        self.assertIsNone(provider._get_segment_text())
        self.assertEqual(len(''.join(provider.tts_text_buff)), provider.processed_chars)

    def test_long_first_segment_is_bounded_without_dropping_text(self):
        provider = self.provider()
        full = '海绵宝宝带着派大星一起去比奇堡抓水母然后回家做饭'
        provider.tts_text_buff = [full]
        self.assertEqual(full[:16], provider._get_segment_text())
        self.assertEqual(full[16:], full[provider.processed_chars:])
        self.assertIsNone(provider._get_segment_text())


if __name__ == '__main__':
    unittest.main()
