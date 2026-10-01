# Build log

Local implementation of PLAN.md. Heavy work is skipped and written down here. Nothing in v1 was heavy enough to skip.

## 2026-10-01

- Host: this Mac. Python 3.14.2 (`python3`). No extra packages installed. Tests use the stdlib `unittest`.
- Repository: `/Users/axon_dendrite/sim-sdk`, git initialized here. Not pushed. Publishing was not asked for.
- Socket guard: a probe of `socket.create_connection(("203.0.113.1", 1))` on this interpreter audited `socket.getaddrinfo`, then `socket.__new__`, then `socket.connect`, and the connect waited until timeout. Raising from the `getaddrinfo` hook aborts before the connect. The proof therefore expects op `socket.getaddrinfo`. PLAN.md was updated to match. `datetime.datetime.now` cannot be assigned on the C type (`TypeError: cannot set 'now' attribute`). The guard replaces `datetime.datetime` on the module with a subclass. `from datetime import datetime` after the guard sees the subclass. A binding taken before the guard is a known hole, same as any import-time client.
- Skipped: none. No GitHub remote, no install of setuptools, no network calls in the suite.
