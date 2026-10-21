"""Incremental PCM WAV decoding without buffering a complete utterance."""
import struct


class WavStream:
    def __init__(self):
        self.buffer = bytearray()
        self.sample_rate = None
        self.started = False
        self.riff = False
        self.audio_bytes = 0
        self.header_bytes = 0

    def feed(self, data):
        self.buffer.extend(data)
        if not self.riff:
            if len(self.buffer) < 12:
                return b""
            if self.buffer[:4] != b"RIFF" or self.buffer[8:12] != b"WAVE":
                raise ValueError("TTS stream is not a WAV file")
            del self.buffer[:12]
            self.riff = True
        while not self.started:
            if len(self.buffer) < 8:
                return b""
            tag, size = struct.unpack_from("<4sI", self.buffer)
            if tag == b"data":
                if self.sample_rate is None:
                    raise ValueError("WAV data precedes format")
                del self.buffer[:8]
                # GPT-SoVITS deliberately emits a zero-length streaming header.
                self.started = True
                break
            if size > 65536 or self.header_bytes + size > 65536:
                raise ValueError("WAV metadata exceeds limit")
            padded = size + (size & 1)
            if len(self.buffer) < 8 + padded:
                return b""
            if tag == b"fmt ":
                if size < 16:
                    raise ValueError("Incomplete WAV format")
                encoding, channels, rate, _, alignment, bits = struct.unpack_from("<HHIIHH", self.buffer, 8)
                if encoding != 1 or channels != 1 or bits != 16 or alignment != 2 or not 8000 <= rate <= 192000:
                    raise ValueError("TTS stream must be mono 16-bit PCM")
                self.sample_rate = rate
            del self.buffer[:8 + padded]
            self.header_bytes += 8 + padded
        length = len(self.buffer) & ~1
        pcm = bytes(self.buffer[:length])
        del self.buffer[:length]
        self.audio_bytes += length
        return pcm

    def finish(self):
        if not self.started or not self.audio_bytes or self.buffer:
            raise ValueError("Empty or truncated TTS WAV stream")


class StreamingOpus:
    def __init__(self, input_rate, output_rate, encoder, callback):
        import numpy as np
        self.np, self.encoder, self.callback = np, encoder, callback
        self.resampler = None
        if input_rate != output_rate:
            import soxr
            self.resampler = soxr.ResampleStream(input_rate, output_rate, 1, dtype="int16", quality="HQ")

    def feed(self, pcm=b"", last=False):
        samples = self.np.frombuffer(pcm, dtype="<i2")
        if self.resampler is not None:
            samples = self.resampler.resample_chunk(samples, last=last)
        self.encoder.encode_pcm_to_opus_stream(samples.astype("<i2", copy=False).tobytes(),
            end_of_stream=last, callback=self.callback)
