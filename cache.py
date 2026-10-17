"""Small in-memory answer cache: TTL plus a FIFO limit on the number of entries."""
from collections import OrderedDict
import time
from typing import Callable

CacheKey = tuple[str, str, str]


class AnswerCache:
    def __init__(self, ttl_seconds: float, max_entries: int,
                 clock: Callable[[], float] = time.monotonic):
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._clock = clock
        self._items: OrderedDict[CacheKey, tuple[str, float]] = OrderedDict()

    def _drop_expired(self) -> None:
        now = self._clock()
        for key in [k for k, (_, expires) in self._items.items() if expires <= now]:
            del self._items[key]

    def get(self, key: CacheKey) -> str | None:
        """Return a stored answer, or None on a miss. A hit does not extend the TTL."""
        self._drop_expired()
        item = self._items.get(key)
        return item[0] if item else None

    def put(self, key: CacheKey, answer: str) -> None:
        """Store a valid answer. When full, remove the entry that was added first (FIFO)."""
        if not answer.strip():
            return
        self._drop_expired()
        self._items.pop(key, None)
        while len(self._items) >= self.max_entries:
            self._items.popitem(last=False)
        self._items[key] = (answer, self._clock() + self.ttl_seconds)

    def __len__(self) -> int:
        self._drop_expired()
        return len(self._items)
