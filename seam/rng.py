"""A counter stream. Not random.Random, and not a float."""

import hashlib

from seam.errors import Fault

_DOMAIN = b"seam.v1.user"
_MASK = 2**64


class Rng:
    def __init__(self, seed):
        if type(seed) is not int or isinstance(seed, bool) or seed < 0 or seed > _MASK - 1:
            raise Fault("bad_value")
        self.seed = seed
        self.counter = 0

    def rand_u64(self):
        raw = hashlib.sha256(
            _DOMAIN + self.seed.to_bytes(8, "little") + self.counter.to_bytes(8, "little")
        ).digest()
        self.counter += 1
        return int.from_bytes(raw[:8], "little")

    def rand_below(self, n):
        if type(n) is not int or isinstance(n, bool) or n <= 0 or n > _MASK:
            raise Fault("bad_value")
        # Always draws at least once, including n == 1, so the stream position does not depend on a shortcut.
        limit = (_MASK // n) * n
        while True:
            draw = self.rand_u64()
            if draw < limit:
                return draw % n
