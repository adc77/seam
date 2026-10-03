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

## 2026-10-01 — guard hardening

- Branch `guard-and-fs-policy`. Suite is now 38 tests, green on Python 3.13.9 (macOS).
- Probed the guard with about forty adversarial handlers instead of trusting the docstrings. Fourteen escapes were real. The worst: a handler reading the clock through libc `clock_gettime` reported `status: passed` while the run digest changed on every replay. That is the one guarantee the library exists to provide, and it was silently false.
- Closed, each verified to fault with the expected code and op, and to leave no side effect on disk: `ctypes.dlopen`/`dlsym`, `time.perf_counter`/`process_time`, the `random.Random` instance methods, and filesystem access. Only `random.Random.random` had been patched before, so a fresh instance walked straight past it.
- Two facts established by probing the interpreter rather than by reading docs: `ctypes.dlopen` and `ctypes.dlsym` do raise audit events, which is what makes libc reachable to the hook; `os.stat` raises none, so it is patched directly. `os.scandir` raises its event when the iterator is consumed, not when it is created.
- The filesystem is now a policy. Reads and writes are refused unless the path is allowlisted. The builtin `open` reports a mode string and `os.open` reports integer flags, so write intent is read differently for each. Paths are exact, never prefixes, and normalized through `realpath`.
- Interpreter directories are allowlisted for reads so a lazy `import` inside a handler still works. Without that, `import subprocess` faulted on the module file before the real leak was reached. Those reads are not recorded as provenance.
- Reads a handler did make are recorded in the artifact under `fs_reads`, deliberately outside the digest. A run that read nothing leaves the key out, so existing artifacts stay byte-comparable.
- Four bugs found while implementing this, all fixed and all worth recording:
  - `os.path.realpath` calls `os.stat`, which is the patched hook, so path resolution recursed until the stack blew. Seam's own resolution now swaps the real `os.stat` back for its duration.
  - `allow_read` before `install_guards` was discarded by the policy reset, so a product could register a path and still be refused. Registrations now survive.
  - Resolving interpreter paths at Policy construction ran before `_norm` and `_REAL_STAT` were defined. Both constants and the policy construction moved below their first use.
  - Interpreter paths were only populated on reset, so `is_interpreter` was wrong whenever it was consulted before `install_guards`.
- Two existing tests asserted the old permissive behaviour (a handler writing any file, a test reading the artifact directly). Both now express the policy: the test's own I/O runs inside `seam.guard.trusted()`, which is what that context manager is for.
- Skipped: none. CI added in `.github/workflows/test.yml`: the suite on ubuntu and macos against 3.13 and 3.14, the checkout proof with its pinned digest, a wheel build that installs and ships the proof cases, and a job asserting the audit events the guard depends on actually fire on the runner. That last one exists because a green suite on a platform missing an event would be a false green. Every CI command was run locally before being committed; the `os.scandir` check failed on the first attempt and was corrected.

## 2026-10-01 — reviewing the guard as an attacker

- The first CI run came back failed with all four suite jobs green. The `guard` job referenced `${{ matrix.os }}` but declared no `strategy.matrix`, so GitHub could not expand it and dropped the job. Fixed; it now runs on both platforms.
- Then I reviewed this branch's own guard as an attacker rather than its author, and found six ways a handler could reach the host. Three were real breaches of the policy this branch introduced, and all three reported `passed`:
  - `allow_read(SECRET)` from inside a handler, then reading it.
  - `with trusted():` reading it.
  - Setting `POLICY.armed = False`, which disabled the filesystem policy outright.
- `allow_read`, `allow_write` and `trusted()` exist for a product to call while wiring up its runtime, and nothing stopped a handler calling them mid-run. The policy now seals at install time and refuses all three from inside a handler with `file_access`.
- `POLICY.armed` was a boolean any handler could clear. It is gone: the audit hook tests a token that only a real `trusted()` block sets, so there is no public switch to flip.
- The fourth hole was mine. Saving the real `os.stat` so that `realpath` could work left it importable from `seam.guard`, and a handler could read host file metadata straight into a port request and into the digest, with the run still `passed`. It is now closed over by `_make_sealed_stat` and refuses to work inside a handler.
- One subtlety worth recording: `_check_stat` normalises its argument through `_norm`, which is seam's own work happening inside the handler's window. The sealed stat therefore also checks `_resolving`, or seam refuses its own path handling and every filesystem fault becomes `file_access`.
- Ordering bit twice more while fixing this: `_make_sealed_stat` and the `POLICY` construction each need to come after the other. Both are now defined below the helpers they use.
- A regression test runs all six attempts and asserts the host value appears nowhere in the artifact. Suite is 39 tests, green.
- Still true after all of this: the guard refuses the listed standard-library paths and does not contain a process. A name bound before `install_guards()` still points at the original object, and a C extension can reach libc without `ctypes`. The README says so.

## 2026-10-01 — mutation testing the suites

- A green suite proves nothing if it cannot fail, so I mutated 21 behaviours one
  at a time and required the suite to go red for each. 19 were caught on the
  first mutation.
- Two survived, and both were my mutation being dishonest rather than a test
  gap. `socket` is blocked twice, by the audit hook and by the direct
  monkeypatch; removing one leaves the other, and only removing both turns the
  suite red. `allow_read` and `allow_write` share one refusal, and disabling a
  single occurrence left the other to catch it. Disabling both, or both socket
  defences, turns the suite red as expected.
- So the suite does pin these guarantees; it just cannot tell which of two
  redundant defences is doing the work. That is acceptable for defence in depth
  and worth recording so nobody later reads a surviving mutant as a free pass.
- Two mutations were skipped because the pattern did not match; they were
  rewritten as no-op edits and are not claimed as evidence either way.
- Independent battery, all executed rather than assumed: 17/17 checks. Digest
  stability across three separate process restarts for all three proof cases;
  the pinned run and case digests still hold; a product using only ctx for time,
  rng, ids and timers is stable across three runs; live mode still has working
  wall clock, perf_counter, monotonic, state, timers, ports and recording;
  glass passes from a clean clone both as a sibling and via SEAM_SDK_PATH; both
  trees clean with no build output tracked.
- One battery check failed at first and the code was right: calling
  `rt.emit(...)` outside a handler is refused by design on `main` as well as on
  this branch, so the check was wrong, not the guard.
