"""Min-heap of (at_ns, seq, handler, body, token). Same-time order is seq, not an RNG."""

import heapq


class Queue:
    def __init__(self):
        self._heap = []
        self._seq = 0
        self._cancelled = set()
        self.side = []

    def push_arrival(self, at_ns, handler, body):
        seq = self._seq
        self._seq += 1
        heapq.heappush(self._heap, (at_ns, seq, handler, body, None))
        return seq

    def push_timer(self, at_ns, handler, body, token):
        """Armed timers wait in `side` until the current handler returns."""
        seq = self._seq
        self._seq += 1
        self.side.append((at_ns, seq, handler, body, token))
        return seq

    def cancel(self, seq, token):
        self._cancelled.add(seq)
        self.side = [item for item in self.side if item[4] != token]

    def flush(self):
        for item in self.side:
            heapq.heappush(self._heap, item)
        self.side.clear()

    def peek(self):
        while self._heap and self._heap[0][1] in self._cancelled:
            heapq.heappop(self._heap)
        if not self._heap:
            return None
        return self._heap[0]

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
        return {"next_seq": self._seq,
                "entries": [list(item) for item in sorted(self._heap) if item[1] not in self._cancelled]}

    def restore(self, snapshot):
        """Restore an already-validated scheduler snapshot without renumbering work."""
        self._seq = snapshot["next_seq"]
        self._heap = [tuple(item) for item in snapshot["entries"]]
        heapq.heapify(self._heap)
        self._cancelled.clear()
        self.side.clear()
