"""Small, atomic local preferences. Credentials never belong in this file."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import tempfile


def _validated(values: object) -> dict:
    result = {'volume': .75, 'quality': 'hq', 'equalizer': (0.0,) * 10,
              'visualization': 'ribbons', 'shuffle': False, 'repeat': False,
              'equalizer_expanded': False, 'window_size': (960, 800),
              'window_maximized': False}
    if not isinstance(values, dict):
        return result
    volume = values.get('volume')
    if type(volume) in (float, int) and 0 <= volume <= 1 and math.isfinite(volume):
        result['volume'] = float(volume)
    quality = values.get('quality')
    if isinstance(quality, str) and quality in ('nq', 'hq', 'lossless'):
        result['quality'] = quality
    gains = values.get('equalizer')
    if (isinstance(gains, (list, tuple)) and len(gains) == 10
            and all(type(gain) in (int, float) and -12 <= gain <= 12
                    and math.isfinite(gain) for gain in gains)):
        result['equalizer'] = tuple(float(gain) for gain in gains)
    mode = values.get('visualization')
    if isinstance(mode, str) and mode in ('ribbons', 'bars', 'mirror', 'orbit'):
        result['visualization'] = mode
    for name in ('shuffle', 'repeat', 'equalizer_expanded', 'window_maximized'):
        if type(values.get(name)) is bool:
            result[name] = values[name]
    size = values.get('window_size')
    if (isinstance(size, (list, tuple)) and len(size) == 2
            and all(type(dimension) is int for dimension in size)
            and 480 <= size[0] <= 16384 and 360 <= size[1] <= 16384):
        result['window_size'] = tuple(size)
    return result


class Settings:
    def __init__(self, path: str | Path | None = None):
        config = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
        self.path = Path(path) if path is not None else config / 'impuls' / 'settings.json'

    def load(self) -> dict:
        try:
            # Preferences are tiny; do not accidentally read an unbounded file.
            with self.path.open(encoding='utf-8') as source:
                raw = source.read(16385)
            values = json.loads(raw) if len(raw) <= 16384 else {}
        except (OSError, ValueError, RecursionError):
            values = {}
        return _validated(values)

    def save(self, values: dict) -> bool:
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                             dir=self.path.parent, prefix='.settings-',
                                             delete=False) as output:
                temporary = Path(output.name)
                json.dump(_validated(values), output, ensure_ascii=False, allow_nan=False)
                output.write('\n')
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            return True
        except (OSError, ValueError):
            return False
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
