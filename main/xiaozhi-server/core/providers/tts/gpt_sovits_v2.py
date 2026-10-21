import requests
import asyncio
import threading
import aiohttp
from config.logger import setup_logging
from core.providers.tts.base import TTSProviderBase
from core.providers.tts.dto.dto import SentenceType
from core.providers.tts.wav_stream import WavStream, StreamingOpus
from core.utils.tts import MarkdownCleaner
from core.utils.util import parse_string_to_list

TAG = __name__
logger = setup_logging()


class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.url = config.get("url")
        self.text_lang = config.get("text_lang", "zh")
        self.ref_audio_path = config.get('ref_audio') if config.get('ref_audio') else config.get("ref_audio_path")
        self.prompt_text = config.get('ref_text') if config.get('ref_text') else config.get("prompt_text")
        self.prompt_lang = config.get("prompt_lang", "zh")

        # 处理空字符串的情况
        top_k = config.get("top_k", "5")
        top_p = config.get("top_p", "1")
        temperature = config.get("temperature", "1")
        batch_threshold = config.get("batch_threshold", "0.75")
        batch_size = config.get("batch_size", "1")
        speed_factor = config.get("speed_factor", "1.0")
        seed = config.get("seed", "-1")
        repetition_penalty = config.get("repetition_penalty", "1.35")

        self.top_k = int(top_k) if top_k else 5
        self.top_p = float(top_p) if top_p else 1
        self.temperature = float(temperature) if temperature else 1
        self.batch_threshold = float(batch_threshold) if batch_threshold else 0.75
        self.batch_size = int(batch_size) if batch_size else 1
        self.speed_factor = float(speed_factor) if speed_factor else 1.0
        self.seed = int(seed) if seed else -1
        self.repetition_penalty = (
            float(repetition_penalty) if repetition_penalty else 1.35
        )

        self.text_split_method = config.get("text_split_method", "cut0")

        self.split_bucket = str(config.get("split_bucket", True)).lower() in (
            "true",
            "1",
            "yes",
        )
        self.return_fragment = str(config.get("return_fragment", False)).lower() in (
            "true",
            "1",
            "yes",
        )

        mode = config.get("streaming_mode", 0)
        if isinstance(mode, str) and mode.lower() in {"true", "false", "yes", "no"}:
            mode = mode.lower() in {"true", "yes"}
        self.streaming_mode = int(mode)
        if self.streaming_mode not in (0, 1, 2, 3):
            raise ValueError("streaming_mode must be 0, 1, 2 or 3")
        self.min_chunk_length = max(8, min(100, int(config.get("min_chunk_length", 16))))
        self.first_sentence_max_chars = max(12, min(80, int(config.get("first_sentence_max_chars", 32))))
        self._stream_guard = threading.Lock()
        self._stream_task = self._stream_loop = None

        self.parallel_infer = str(config.get("parallel_infer", True)).lower() in (
            "true",
            "1",
            "yes",
        )

        self.aux_ref_audio_paths = parse_string_to_list(
            config.get("aux_ref_audio_paths")
        )
        self.audio_file_type = config.get("format", "wav")

    def _request_json(self, text, mode=0):
        return {
            "text": text,
            "text_lang": self.text_lang,
            "ref_audio_path": self.ref_audio_path,
            "aux_ref_audio_paths": self.aux_ref_audio_paths,
            "prompt_text": self.prompt_text,
            "prompt_lang": self.prompt_lang,
            "top_k": self.top_k,
            "top_p": self.top_p,
            "temperature": self.temperature,
            "text_split_method": self.text_split_method,
            "batch_size": self.batch_size,
            "batch_threshold": self.batch_threshold,
            "split_bucket": self.split_bucket,
            "return_fragment": self.return_fragment,
            "speed_factor": self.speed_factor,
            "streaming_mode": mode,
            "media_type": "wav",
            "min_chunk_length": self.min_chunk_length,
            "seed": self.seed,
            "parallel_infer": self.parallel_infer,
            "repetition_penalty": self.repetition_penalty,
        }

    async def text_to_speak(self, text, output_file):
        with requests.post(self.url, json=self._request_json(text), timeout=self.tts_timeout) as resp:
            resp.raise_for_status()
            if output_file:
                with open(output_file, "wb") as file:
                    file.write(resp.content)
            else:
                return resp.content

    def cancel_synthesis(self):
        with self._stream_guard:
            loop, task = self._stream_loop, self._stream_task
        if loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # The worker already completed and closed its event loop.

    def to_tts_stream(self, text, opus_handler=None):
        cleaned = MarkdownCleaner.clean_markdown(text)
        # LLM chunks can leave a trailing question mark as a separate segment.
        # GPT-SoVITS cannot synthesize punctuation alone; do not send/retry it.
        if not any(character.isalnum() for character in cleaned):
            return
        if not self.streaming_mode or not self.delete_audio_file:
            return super().to_tts_stream(text, opus_handler)
        turn = getattr(self, "current_sentence_id", None) or self.conn.sentence_id
        emitted = False
        failed = False
        if self._correct_words_pattern:
            cleaned = self._correct_words_pattern.sub(lambda match: self.correct_words[match.group(0)], cleaned)

        def cancelled():
            return self.conn.client_abort or self.conn.stop_event.is_set() or self.conn.sentence_id != turn

        def emit(packet):
            nonlocal emitted
            if cancelled():
                raise asyncio.CancelledError()
            if not emitted:
                self.tts_audio_queue.put((SentenceType.FIRST, None, text, turn))
                emitted = True
            if opus_handler:
                opus_handler(packet)
            else:
                self.tts_audio_queue.put((SentenceType.MIDDLE, packet, None, turn))

        async def synthesize():
            task = asyncio.current_task()
            with self._stream_guard:
                self._stream_loop, self._stream_task = asyncio.get_running_loop(), task
            try:
                if cancelled():
                    return
                parser, encoder = WavStream(), None
                timeout = aiohttp.ClientTimeout(total=None, connect=3, sock_read=self.tts_timeout)
                async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as client:
                    async with client.post(self.url, json=self._request_json(cleaned, self.streaming_mode)) as response:
                        response.raise_for_status()
                        async for data in response.content.iter_chunked(4096):
                            if cancelled():
                                return
                            pcm = parser.feed(data)
                            if pcm:
                                if encoder is None:
                                    encoder = StreamingOpus(parser.sample_rate, self.conn.sample_rate, self.opus_encoder, emit)
                                encoder.feed(pcm)
                        parser.finish()
                        if not cancelled():
                            encoder.feed(last=True)
            finally:
                with self._stream_guard:
                    if self._stream_task is task:
                        self._stream_loop = self._stream_task = None

        try:
            asyncio.run(synthesize())
        except asyncio.CancelledError:
            # Explicit cancellation must never retry or replay the old sentence.
            return
        except Exception as error:
            failed = True
            if cancelled():
                return
            if emitted:
                logger.bind(tag=TAG).warning(f"流式语音中断，保留已播放部分，不重复播报: {type(error).__name__}")
            else:
                logger.bind(tag=TAG).warning(f"流式语音暂不可用，回退普通合成: {type(error).__name__}")
                super().to_tts_stream(text, opus_handler)
        finally:
            if cancelled() or failed:
                self.opus_encoder.reset_state()

    def _get_segment_text(self):
        full = "".join(self.tts_text_buff)
        remaining = full[self.processed_chars:]
        punctuation = self.first_sentence_punctuations if self.is_first_sentence else self.punctuations
        positions = [i for i, char in enumerate(remaining) if char in punctuation and i >= (5 if self.is_first_sentence else 0)]
        length = positions[0] + 1 if positions else 0
        if self.is_first_sentence and (not length or length > self.first_sentence_max_chars):
            if len(remaining) >= self.first_sentence_max_chars:
                length = self.first_sentence_max_chars
        if not length:
            return None
        self.processed_chars += length
        self.is_first_sentence = False
        from core.utils import textUtils
        return textUtils.get_string_no_punctuation_or_emoji(remaining[:length])

    async def close(self):
        self.cancel_synthesis()
        await super().close()
