"""Small, session-only collections of timestamp bookmarks."""


class MomentBook:
    """Keep up to five exact positions for each of 32 recent track entries."""

    def __init__(self):
        self._moments: dict[str, tuple[int, ...]] = {}

    def positions(self, track_id: str) -> tuple[int, ...]:
        return self._moments.get(track_id, ())

    def add(self, track_id: str, position_ms: int, duration_ms: int) -> str:
        if (not isinstance(track_id, str) or not track_id
                or not isinstance(position_ms, int) or isinstance(position_ms, bool)
                or not isinstance(duration_ms, int) or isinstance(duration_ms, bool)
                or not 0 <= position_ms < duration_ms):
            return 'unavailable'
        positions = self.positions(track_id)
        if any(position // 1000 == position_ms // 1000 for position in positions):
            return 'duplicate'
        if len(positions) >= 5:
            return 'full'
        if track_id not in self._moments and len(self._moments) >= 32:
            del self._moments[next(iter(self._moments))]
        self._moments[track_id] = tuple(sorted((*positions, position_ms)))
        return 'added'

    def clear(self, track_id: str) -> None:
        self._moments.pop(track_id, None)
