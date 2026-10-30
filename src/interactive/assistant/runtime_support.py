"""Small per-turn resources and short-lock caches; no independent worker loop."""

import threading
from collections import OrderedDict


class LockedOrderedDict(OrderedDict):
    """Return snapshots for iteration; never hold a lock across model calls."""

    def __init__(self):
        super().__init__()
        self._lock = threading.RLock()

    def get(self, key, default=None):
        with self._lock:
            return super().get(key, default)

    def __setitem__(self, key, value):
        with self._lock:
            return super().__setitem__(key, value)

    def pop(self, key, *default):
        with self._lock:
            return super().pop(key, *default)

    def popitem(self, last=True):
        with self._lock:
            return super().popitem(last=last)

    def move_to_end(self, key, last=True):
        with self._lock:
            # Another session may have expired this key after a snapshot.
            if key in self:
                super().move_to_end(key, last=last)

    def items(self):
        with self._lock:
            return list(super().items())

    def __len__(self):
        with self._lock:
            return super().__len__()
