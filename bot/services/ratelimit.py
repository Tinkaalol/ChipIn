import time
from collections import defaultdict, deque
from collections.abc import Callable


class RateLimiter:
    """Не больше `limit` событий за `window` секунд на ключ. Состояние в памяти процесса."""

    def __init__(
        self, limit: int, window: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.limit = limit
        self.window = window
        self._clock = clock
        self._events: dict[int, deque[float]] = defaultdict(deque)

    def clear(self) -> None:
        self._events.clear()

    def allow(self, key: int) -> bool:
        """Фиксирует событие и возвращает True, если лимит не превышен."""
        now = self._clock()
        events = self._events[key]
        while events and now - events[0] >= self.window:
            events.popleft()
        if len(events) >= self.limit:
            return False
        events.append(now)
        return True


# Заявки: не больше 10 в час; создание групп: не больше 3 в час
application_limiter = RateLimiter(limit=10, window=3600)
group_limiter = RateLimiter(limit=3, window=3600)

RATE_LIMIT_MESSAGE = "Слишком часто. Подождите немного и попробуйте снова."
