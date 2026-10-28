# gemini_recorder.py
# Replacement recorder for Gemini Live / Avaya Infinity RCMS bridge.
#
# Purpose:
# - Records customer PCM (16 kHz, signed 16-bit little-endian, mono)
# - Records Gemini PCM (24 kHz, signed 16-bit little-endian, mono)
# - Writes one combined 24 kHz, 16-bit, mono WAV file
# - Uses the timestamp of the FIRST chunk from each source to align the tracks
# - Places later chunks sequentially on their own track instead of positioning
#   every chunk by callback-arrival time (which can introduce jitter/gaps)
# - Uses a soft limiter during mixing to reduce hard clipping
#
# IMPORTANT:
# This file is a standalone replacement implementation. Check the existing
# GeminiRecorder(...) constructor call in bot_gemini.py and make its arguments
# match the constructor below. The methods add_customer_pcm(), add_gemini_pcm(),
# and close() are compatible with the integration pattern discussed.
#
# Recommended call-site changes in bot_gemini.py:
#
# 1) Customer audio:
#    pcm_16k = self._prepare_input_audio(...)
#    audio_timestamp = time.monotonic()  # immediately before sending to Gemini
#    await convo.gm_ws.send(json.dumps(msg))
#    if convo.recorder is not None:
#        convo.recorder.add_customer_pcm(pcm_16k, timestamp=audio_timestamp)
#
#    Better, to record the same audio even if the send fails, call add_customer_pcm
#    just before the await, after pcm_16k is prepared:
#        if convo.recorder is not None:
#            convo.recorder.add_customer_pcm(pcm_16k, timestamp=time.monotonic())
#        await convo.gm_ws.send(json.dumps(msg))
#
# 2) Gemini audio:
#    pcm_24k = base64.b64decode(b64)
#    if convo.recorder is not None:
#        convo.recorder.add_gemini_pcm(pcm_24k, timestamp=time.monotonic())
#
# A timestamp anchors only the first chunk of each track. This is best-effort
# alignment based on when the bridge receives/processes audio. It cannot provide
# exact acoustic synchronization if the upstream API does not provide source
# timestamps or playback timing.

from __future__ import annotations

import logging
import math
import os
import tempfile
import threading
import time
import wave
from array import array
from pathlib import Path
from typing import Optional, Union

logger = logging.getLogger(__name__)

PathLike = Union[str, os.PathLike]


class GeminiRecorder:
    """Record two PCM streams into a single mixed mono WAV file.

    Args:
        output_path: Full path of the final WAV file.
        customer_rate: Customer input sample rate (normally 16000).
        gemini_rate: Gemini output sample rate (normally 24000).
        output_rate: Output WAV sample rate (normally 24000).
        normalize_peak: If True, scale the final mix to a safe peak.
        peak_target: Peak target as a fraction of full scale (0.95 = -0.45 dBFS).
    """

    OUTPUT_RATE = 24000
    SAMPLE_WIDTH_BYTES = 2
    CHANNELS = 1

    def __init__(
        self,
        output_path: PathLike,
        customer_rate: int = 16000,
        gemini_rate: int = 24000,
        output_rate: int = 24000,
        normalize_peak: bool = True,
        peak_target: float = 0.95,
    ) -> None:
        self.output_path = Path(output_path)
        self.customer_rate = int(customer_rate)
        self.gemini_rate = int(gemini_rate)
        self.output_rate = int(output_rate)
        self.normalize_peak = bool(normalize_peak)
        self.peak_target = max(0.1, min(float(peak_target), 0.99))

        if self.customer_rate <= 0 or self.gemini_rate <= 0 or self.output_rate <= 0:
            raise ValueError("Sample rates must be positive integers")

        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._started_at = time.monotonic()

        # Each source is kept as a sequential PCM spool. This avoids positioning
        # every individual chunk by its arrival timestamp.
        self._spools = {
            "customer": tempfile.TemporaryFile(mode="w+b"),
            "gemini": tempfile.TemporaryFile(mode="w+b"),
        }
        self._track_first_timestamp: dict[str, Optional[float]] = {
            "customer": None,
            "gemini": None,
        }
        self._track_bytes: dict[str, int] = {"customer": 0, "gemini": 0}
        self._track_rates = {
            "customer": self.customer_rate,
            "gemini": self.gemini_rate,
        }

    def add_customer_pcm(self, pcm_bytes: bytes, timestamp: Optional[float] = None) -> None:
        """Append one customer PCM chunk (S16LE mono, normally 16 kHz)."""
        self._add("customer", pcm_bytes, timestamp)

    def add_gemini_pcm(self, pcm_bytes: bytes, timestamp: Optional[float] = None) -> None:
        """Append one Gemini PCM chunk (S16LE mono, normally 24 kHz)."""
        self._add("gemini", pcm_bytes, timestamp)

    def _add(self, source: str, pcm_bytes: bytes, timestamp: Optional[float]) -> None:
        if not pcm_bytes:
            return
        data = bytes(pcm_bytes)
        # S16LE PCM must contain whole 16-bit samples.
        if len(data) % 2:
            data = data[:-1]
        if not data:
            return

        with self._lock:
            if self._closed:
                logger.debug("Ignoring %s PCM after recorder was closed", source)
                return

            if self._track_first_timestamp[source] is None:
                # timestamp is from time.monotonic() when supplied by caller.
                # If absent, use current time as a best-effort anchor.
                self._track_first_timestamp[source] = (
                    float(timestamp) if timestamp is not None else time.monotonic()
                )

            spool = self._spools[source]
            spool.seek(0, os.SEEK_END)
            spool.write(data)
            self._track_bytes[source] += len(data)

    @staticmethod
    def _bytes_to_samples(pcm_bytes: bytes) -> array:
        samples = array("h")
        samples.frombytes(pcm_bytes)
        # array('h') is native-endian. Convert to/from little endian explicitly
        # on big-endian systems so WAV samples remain S16LE.
        if os.sys.byteorder != "little":
            samples.byteswap()
        return samples

    @staticmethod
    def _samples_to_bytes(samples: array) -> bytes:
        out = array("h", samples)
        if os.sys.byteorder != "little":
            out.byteswap()
        return out.tobytes()

    @staticmethod
    def _resample_linear(samples: array, src_rate: int, dst_rate: int) -> array:
        """Simple linear resampler. Output length is rounded to sample duration."""
        if not samples:
            return array("h")
        if src_rate == dst_rate:
            return array("h", samples)

        out_len = max(1, int(round(len(samples) * dst_rate / src_rate)))
        out = array("h")
        last = len(samples) - 1

        for i in range(out_len):
            # Map output sample position to source sample position.
            pos = i * src_rate / dst_rate
            left = min(int(pos), last)
            right = min(left + 1, last)
            frac = pos - left
            value = samples[left] + (samples[right] - samples[left]) * frac
            out.append(max(-32768, min(32767, int(round(value)))))
        return out

    def _read_track_at_output_rate(self, source: str) -> tuple[array, int]:
        """Return resampled samples and the track's start sample on output timeline."""
        spool = self._spools[source]
        spool.seek(0)
        pcm = spool.read()
        samples = self._bytes_to_samples(pcm)
        converted = self._resample_linear(samples, self._track_rates[source], self.output_rate)

        first_ts = self._track_first_timestamp[source]
        if first_ts is None:
            return converted, 0

        # Align the first chunk of each track to the common recorder clock.
        # Subsequent chunks are sequential, preserving within-track timing.
        start_seconds = max(0.0, first_ts - self._started_at)
        start_sample = int(round(start_seconds * self.output_rate))
        return converted, start_sample

    @staticmethod
    def _soft_limit(value: float, limit: float = 32700.0) -> int:
        """Softly limit peaks rather than hard-clipping the summed waveform."""
        if value > limit:
            value = limit + (32767.0 - limit) * math.tanh((value - limit) / (32767.0 - limit))
        elif value < -limit:
            value = -limit + (32767.0 - limit) * math.tanh((value + limit) / (32767.0 - limit))
        return max(-32768, min(32767, int(round(value))))

    def close(self) -> list[str]:
        """Finalize the WAV file and return [output_path] for call-site compatibility."""
        with self._lock:
            if self._closed:
                return [str(self.output_path)]
            self._closed = True

            try:
                customer, customer_start = self._read_track_at_output_rate("customer")
                gemini, gemini_start = self._read_track_at_output_rate("gemini")

                end_customer = customer_start + len(customer)
                end_gemini = gemini_start + len(gemini)
                total_samples = max(end_customer, end_gemini, 0)

                if total_samples == 0:
                    mixed = array("h")
                else:
                    # Use float accumulator so overlapping streams do not overflow.
                    mix = [0.0] * total_samples
                    for i, sample in enumerate(customer):
                        mix[customer_start + i] += float(sample)
                    for i, sample in enumerate(gemini):
                        mix[gemini_start + i] += float(sample)

                    peak = max((abs(v) for v in mix), default=0.0)
                    if self.normalize_peak and peak > 0:
                        gain = (32767.0 * self.peak_target) / peak
                    else:
                        gain = 1.0

                    mixed = array("h", (self._soft_limit(v * gain) for v in mix))

                pcm_out = self._samples_to_bytes(mixed)
                temp_path = self.output_path.with_suffix(self.output_path.suffix + ".tmp")

                with wave.open(str(temp_path), "wb") as wav:
                    wav.setnchannels(self.CHANNELS)
                    wav.setsampwidth(self.SAMPLE_WIDTH_BYTES)
                    wav.setframerate(self.output_rate)
                    wav.writeframes(pcm_out)

                os.replace(temp_path, self.output_path)
                logger.info(
                    "Gemini mixed recording saved: %s (rate=%d, samples=%d, duration=%.2fs)",
                    self.output_path,
                    self.output_rate,
                    len(mixed),
                    len(mixed) / self.output_rate,
                )
                return [str(self.output_path)]

            finally:
                for spool in self._spools.values():
                    try:
                        spool.close()
                    except Exception:
                        logger.debug("Unable to close recorder spool", exc_info=True)

    def __enter__(self) -> "GeminiRecorder":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
