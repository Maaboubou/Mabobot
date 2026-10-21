"""Provider output throughput measured over client-observed model output."""
import math
import time


MIN_GENERATION_SECONDS = 0.1


class GenerationTiming:
    def __init__(self, started=None):
        self.started = time.monotonic() if started is None else started
        self.first = None  # First visible text, independent of reasoning/tool output.
        self.last = None
        self.first_output = None
        self.completed = None
        self.text_chunk_count = 0
        self.interrupted = False

    def observe_output(self, now=None):
        if self.first_output is None:
            self.first_output = time.monotonic() if now is None else now

    def observe(self, text, now=None):
        if not isinstance(text, str) or not text:
            return
        now = time.monotonic() if now is None else now
        self.observe_output(now)
        if self.first is None:
            self.first = now
        self.last = now
        self.text_chunk_count += 1

    def finish(self, now=None):
        # Freeze the event boundary; later accounting must not extend the interval.
        if self.completed is None:
            self.completed = time.monotonic() if now is None else now

    def snapshot(self, *, usage=None, complete=True):
        seconds = (self.completed - self.first_output
                   if self.completed is not None and self.first_output is not None else None)
        result = {
            'source': 'client_stream',
            'method': 'provider_output_over_decode_window',
            'first_token_seconds': None if self.first is None else max(0, self.first - self.started),
            'first_output_seconds': None if self.first_output is None else max(0, self.first_output - self.started),
            'last_token_seconds': None if self.last is None else max(0, self.last - self.started),
            'generation_seconds': seconds,
            'generation_tokens_per_second': None,
            'text_chunk_count': self.text_chunk_count,
            'reason': 'missing_usage',
        }
        if not complete:
            result['reason'] = 'incomplete_stream'
            return result
        if self.interrupted:
            result['reason'] = 'non_text_output'
            return result
        if seconds is None or not math.isfinite(seconds) or seconds <= 0:
            result['reason'] = 'missing_output_timing'
            return result
        if seconds < MIN_GENERATION_SECONDS:
            result['reason'] = 'insufficient_observation_window'
            return result
        if not usage or usage.get('estimated'):
            return result
        output = usage.get('completion_tokens', usage.get('output_tokens'))
        if (isinstance(output, bool) or not isinstance(output, (int, float))
                or not math.isfinite(output) or output < 0 or int(output) != output):
            return result
        speed = output / seconds
        if not math.isfinite(speed):
            return result
        result.update(
            generation_tokens_per_second=round(speed, 2),
            generation_tokens=int(output),
            token_count_source='provider_usage',
            estimated=True,
            estimation_reason='client_stream_timing',
            reason=None,
        )
        return result
