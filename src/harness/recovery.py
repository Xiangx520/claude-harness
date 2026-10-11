"""Retry rate limits and overloads with the original fallback policy."""

import random
import time


class RecoveryState:
    def __init__(self, primary_model: str):
        self.has_escalated = False
        self.recovery_count = 0
        self.consecutive_529 = 0
        self.has_attempted_reactive_compact = False
        self.current_model = primary_model


class RecoveryManager:
    """Retry rate limits and overloads with the original fallback policy."""

    def __init__(self, config):
        self.config = config

    def retry_delay(self, attempt: int) -> float:
        base = min(self.config.BASE_DELAY_MS * (2 ** attempt), 32000) / 1000
        return base + random.uniform(0, base * 0.25)

    def with_retry(self, fn, state: RecoveryState):
        for attempt in range(self.config.MAX_RETRIES):
            try:
                result = fn()
                state.consecutive_529 = 0
                return result
            except Exception as e:
                name = type(e).__name__.lower()
                msg = str(e).lower()
                if "ratelimit" in name or "429" in msg:
                    delay = self.retry_delay(attempt)
                    print(f"  \033[33m[429] retry {attempt + 1}/{self.config.MAX_RETRIES} "
                          f"after {delay:.1f}s\033[0m")
                    time.sleep(delay)
                    continue
                if "overloaded" in name or "529" in msg or "overloaded" in msg:
                    state.consecutive_529 += 1
                    if state.consecutive_529 >= self.config.MAX_CONSECUTIVE_529 and self.config.FALLBACK_MODEL:
                        state.current_model = self.config.FALLBACK_MODEL
                        state.consecutive_529 = 0
                        print(f"  \033[31m[529] switching to {self.config.FALLBACK_MODEL}\033[0m")
                    delay = self.retry_delay(attempt)
                    print(f"  \033[33m[529] retry {attempt + 1}/{self.config.MAX_RETRIES} "
                          f"after {delay:.1f}s\033[0m")
                    time.sleep(delay)
                    continue
                raise
        raise RuntimeError(f"Max retries ({self.config.MAX_RETRIES}) exceeded")

    def is_prompt_too_long_error(self, e: Exception) -> bool:
        msg = str(e).lower()
        return (("prompt" in msg and "long" in msg)
                or "context_length_exceeded" in msg
                or "max_context_window" in msg)
