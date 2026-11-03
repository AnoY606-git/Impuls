"""Playback queue with bounded history, shuffle and whole-list repeat."""

from collections import deque
import random


_MAX_HISTORY = 1024


class PlaybackQueue:
    def __init__(self, randomizer=None):
        self._randomizer = randomizer if randomizer is not None else random.Random()
        self._shuffle = False
        self._repeat = False
        self.clear()

    @property
    def current(self):
        if self._position is None:
            return None
        return self._tracks[self._history[self._position]]

    @property
    def shuffle_enabled(self):
        return self._shuffle

    @property
    def repeat_enabled(self):
        return self._repeat

    def replace(self, tracks, selected, index=None):
        self._tracks = tuple(tracks)
        if selected is None:
            self.clear()
            return
        valid_index = (isinstance(index, int) and not isinstance(index, bool)
                       and 0 <= index < len(self._tracks) and self._tracks[index] == selected)
        if not valid_index:
            try:
                index = self._tracks.index(selected)
            except ValueError:
                self._tracks = (selected,)
                index = 0
        # In a normal list, Previous may navigate before the initially selected
        # row; shuffle instead starts its history at the selected track.
        self._history = [index] if self._shuffle else list(range(max(0, index - _MAX_HISTORY + 1), index + 1))
        self._position = len(self._history) - 1
        self._visited = {index}
        self._rebuild_pending()

    def append_wave(self, tracks):
        """Extend an ordered, non-repeating radio queue without losing history."""
        if self._position is None or self._shuffle or self._repeat:
            raise ValueError('Wave continuation requires an active ordered queue')
        items = tuple(tracks)
        start = len(self._tracks)
        self._tracks += items
        self._pending.extend(range(start, start + len(items)))
        # Old played tracks can be dropped once they fall out of bounded history.
        # Keep all still-pending items and remap the surviving indices.
        if len(self._tracks) > _MAX_HISTORY * 2:
            kept = sorted(set(self._history) | set(self._pending))
            indices = {old: new for new, old in enumerate(kept)}
            self._tracks = tuple(self._tracks[old] for old in kept)
            self._history = [indices[old] for old in self._history]
            self._pending = deque(indices[old] for old in self._pending)
            self._visited = {indices[old] for old in self._visited if old in indices}

    def set_shuffle(self, enabled):
        if not isinstance(enabled, bool) or enabled == self._shuffle:
            return
        self._shuffle = enabled
        if self._position is not None:
            del self._history[self._position + 1:]
            self._rebuild_pending()

    def set_repeat(self, enabled):
        if isinstance(enabled, bool):
            self._repeat = enabled

    def _rebuild_pending(self):
        if self._position is None:
            self._pending = deque()
            return
        index = self._history[self._position]
        if self._shuffle:
            candidates = [i for i in range(len(self._tracks)) if i not in self._visited]
            self._randomizer.shuffle(candidates)
        else:
            candidates = list(range(index + 1, len(self._tracks)))
        self._pending = deque(candidates)

    def _new_cycle(self):
        candidates = list(range(len(self._tracks)))
        self._visited.clear()
        if self._shuffle:
            self._randomizer.shuffle(candidates)
            current = self._history[self._position]
            if len(candidates) > 1 and candidates[0] == current:
                candidates[0], candidates[1] = candidates[1], candidates[0]
        self._pending = deque(candidates)

    def next(self):
        if self._position is None:
            return None
        if self._position + 1 < len(self._history):
            self._position += 1
            return self.current
        if not self._pending:
            if not self._repeat:
                return None
            self._new_cycle()
        index = self._pending.popleft()
        self._visited.add(index)
        self._history.append(index)
        if len(self._history) > _MAX_HISTORY:
            del self._history[:len(self._history) - _MAX_HISTORY]
        self._position = len(self._history) - 1
        return self.current

    def previous(self):
        if self._position is None or self._position == 0:
            return None
        self._position -= 1
        index = self._history[self._position]
        self._visited.add(index)
        if self._shuffle:
            try:
                self._pending.remove(index)
            except ValueError:
                pass
        return self.current

    def clear(self):
        self._tracks = ()
        self._history = []
        self._position = None
        self._visited = set()
        self._pending = deque()
