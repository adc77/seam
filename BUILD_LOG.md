# Build log

Local implementation of PLAN.md. Heavy work is skipped and written down here. Nothing in v1 was heavy enough to skip.

## 2026-10-01

- Host: this Mac. Python 3.14.2 (`python3`). No extra packages installed. Tests use the stdlib `unittest`.
- Repository: this one, on `main`. No remote at the time of this entry.
- Socket guard: a probe of `socket.create_connection(("203.0.113.1", 1))` on this interpreter audited `socket.getaddrinfo`, then `socket.__new__`, then `socket.connect`, and the connect waited until timeout. Raising from the `getaddrinfo` hook aborts before the connect. The proof therefore expects op `socket.getaddrinfo`. PLAN.md was updated to match. `datetime.datetime.now` cannot be assigned on the C type (`TypeError: cannot set 'now' attribute`). The guard replaces `datetime.datetime` on the module with a subclass. `from datetime import datetime` after the guard sees the subclass. A binding taken before the guard is a known hole, same as any import-time client.
- Skipped: none. No GitHub remote, no install of setuptools, no network calls in the suite.

## 2026-10-01 — v1 implementation

- Suite: `python3 -m unittest discover -s tests -t .` from the repository root. 33 tests, 0.819s, OK. Python 3.14.2. Stdlib only. `PYTHONPATH` comes from the test helper. Nothing was skipped; every leak kind, the socket grader, and the recording replay ran locally.
- First run failed two tests. `test_tape_rules` used cutoff 0, so the out-of-order tape line was hidden before the order check. Cutoff is now 10 so both lines are visible. Live `schedule_after` sampled `now()` twice, and a 5 ns delay was already in the past on the second sample. It now arms from one sample. Both pass on the re-run.
- Scheduler order, written into PLAN.md: the empty-queue check and the deadline check happen before `max_events`. A run that delivers exactly the budget and then quiesces stops cleanly. `max_port_calls` is checked on emit before the overflowing call, and that call is not logged.
- Pins that the suite checked: declined case digest `396fb7a96493c8190b8665757a8aab43d716098bc911618ddc2307870431a214`, declined run digest `40e9b490276e08ebf25d4865628e8902521b65e83bfb1ab11139c64a8e6146bd`. Seed 1842 draws `10316146753589248087` then `802155294731228146`.
- Parent process never calls `install_guards` or `seam.main`. Those paths run in subprocesses with a 15s timeout, 5s for the socket and subprocess leaks. A missed hook fails the test. The parent `time.time()` stays a float after the suite.
- ResourceWarnings remain from tests that open files without `with`. They do not fail the suite. Library case and tape loads use `with`.
- Skipped: none. No 32 MiB artifact fixture, no setuptools install, no publish, no network. The 32 MiB cap is checked in the writer and was not exercised with a full-size file.
