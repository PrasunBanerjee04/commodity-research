"""Process-local request spacing; each provider supplies its own policy."""

import logging
import math
import threading
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


class RequestGate:
    def __init__(self, minimum_delay: float, *, logger: logging.Logger | None = None):
        if not math.isfinite(minimum_delay) or minimum_delay < 0:
            raise ValueError("minimum_delay must be a finite nonnegative number")
        self.minimum_delay = minimum_delay
        self.logger = logger or logging.getLogger(__name__)
        self._lock = threading.Lock()
        self._last_request: float | None = None

    def run(self, request: Callable[[], T]) -> T:
        with self._lock:
            if self._last_request is not None:
                remaining = self.minimum_delay - (time.monotonic() - self._last_request)
                while remaining > 0:
                    self.logger.info("Rate limit: waiting %.1f seconds", remaining)
                    time.sleep(remaining)
                    remaining = self.minimum_delay - (
                        time.monotonic() - self._last_request
                    )
            self._last_request = time.monotonic()
            return request()
