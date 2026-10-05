"""A stable scheduler with optional timer precedence at equal timestamps."""

import heapq


class Queue:
    def __init__(self, *, timers_first=False):
        self._heap = []
        self.timers_first = timers_first
        self._seq = 0
        self._cancelled = set()
        self.side = []

    def push_arrival(self, at_ns, handler, body):
        seq = self._seq
        self._seq += 1
        heapq.heappush(self._heap, self._entry(at_ns, seq, handler, body, None))
        return seq

    def push_timer(self, at_ns, handler, body, token):
        """Armed timers wait in `side` until the current handler returns."""
        seq = self._seq
        self._seq += 1
        self.side.append(self._entry(at_ns, seq, handler, body, token))
        return seq

    def cancel(self, seq, token):
        self._cancelled.add(seq)
        self.side = [item for item in self.side if item[-1] != token]

    def _entry(self, at_ns, seq, handler, body, token):
        priority = int(token is None) if self.timers_first else 0
        return (at_ns, priority, seq, handler, body, token)

    @staticmethod
    def _public(item):
        return item[:1] + item[2:]

    def flush(self):
        for item in self.side:
            heapq.heappush(self._heap, item)
        self.side.clear()

    def peek(self):
        while self._heap and self._heap[0][2] in self._cancelled:
            heapq.heappop(self._heap)
        if not self._heap:
            return None
        return self._public(self._heap[0])

    def pop(self):
        item = self.peek()
        if item is None:
            return None
        heapq.heappop(self._heap)
        return item

    def snapshot(self):
        """Retain pending work and sequence numbering at a flushed handler boundary."""
        if self.side:
            raise RuntimeError("Cannot checkpoint an unflushed scheduler")
        entries = [self._public(item) for item in self._heap if item[2] not in self._cancelled]
        return {"next_seq": self._seq, "entries": [list(item) for item in sorted(entries)]}

    def restore(self, snapshot):
        """Restore an already-validated scheduler snapshot without renumbering work."""
        self._seq = snapshot["next_seq"]
        self._heap = [self._entry(*item) for item in snapshot["entries"]]
        heapq.heapify(self._heap)
        self._cancelled.clear()
        self.side.clear()
