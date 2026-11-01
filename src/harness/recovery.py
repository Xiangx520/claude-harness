"""Retry rate limits and overloads with the original fallback policy."""

import math
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import time


class RecoveryState:
    def __init__(self, primary_model: str):
        self.has_escalated = False
        self.recovery_count = 0
        self.consecutive_529 = 0
        self.has_attempted_reactive_compact = False
        self.current_model = primary_model
        self.retry_attempt = 0
        self.exhausted = False


class RecoveryManager:
    """Retry rate limits and overloads with the original fallback policy."""

    def __init__(self, config):
        self.config = config

    def retry_delay(self, attempt: int) -> float:
        base = min(self.config.BASE_DELAY_MS * (2 ** attempt), 32000) / 1000
        return base + random.uniform(0, base * 0.25)

    def classify(self, error):
        if self.is_prompt_too_long_error(error):
            return "context"
        status = getattr(error, "status_code", None)
        if isinstance(status, int):
            return "overload" if status == 529 else "transient" if status in {408, 409, 429} or status >= 500 else "fatal"
        name = type(error).__name__.lower()
        msg = str(error).lower()
        if "529" in msg or "overloaded" in name or "overloaded" in msg:
            return "overload"
        if isinstance(error, (ConnectionError, TimeoutError)) or any(x in name for x in ("connection", "timeout", "ratelimit")) or "429" in msg:
            return "transient"
        return "fatal"

    def with_retry(self, fn, state: RecoveryState, checkpoint=None):
        def persist():
            if checkpoint:
                checkpoint()

        def switch():
            fallback = self.config.FALLBACK_MODEL
            if fallback and fallback != state.current_model:
                state.current_model = fallback
                state.retry_attempt = 0
                state.consecutive_529 = 0
                print(f"  [recovery] switching to {fallback}")
                persist()
                return True
            return False

        while True:
            if state.retry_attempt >= self.config.MAX_RETRIES:
                if switch():
                    continue
                state.exhausted = True
                persist()
                raise RuntimeError(f"Max retries ({self.config.MAX_RETRIES}) exceeded; session paused")
            state.retry_attempt += 1
            persist()  # Charge the attempt durably before making the request.
            try:
                result = fn()
            except Exception as exc:
                kind = self.classify(exc)
                if kind not in {"transient", "overload"}:
                    raise
                if kind == "overload":
                    state.consecutive_529 += 1
                    if state.consecutive_529 >= self.config.MAX_CONSECUTIVE_529 and switch():
                        continue
                persist()
                if state.retry_attempt >= self.config.MAX_RETRIES:
                    continue
                delay = self.retry_delay(state.retry_attempt - 1)
                headers = getattr(getattr(exc, "response", None), "headers", {}) or {}
                hint = headers.get("retry-after")
                if hint:
                    try:
                        try:
                            seconds = float(hint)
                        except ValueError:
                            seconds = (parsedate_to_datetime(hint) - datetime.now(timezone.utc)).total_seconds()
                        if math.isfinite(seconds):
                            delay = max(delay, min(60.0, max(0.0, seconds)))
                    except (TypeError, ValueError, OverflowError):
                        pass
                print(f"  [recovery {kind}] attempt {state.retry_attempt}; wait {delay:.1f}s")
                time.sleep(delay)
                continue
            state.retry_attempt = 0
            state.consecutive_529 = 0
            state.exhausted = False
            return result

    def is_prompt_too_long_error(self, e: Exception) -> bool:
        msg = str(e).lower()
        return (("prompt" in msg and "long" in msg)
                or "context_length_exceeded" in msg
                or "max_context_window" in msg)
