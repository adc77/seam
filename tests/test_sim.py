"""Subprocess tests. The parent must not install guards or call seam.main."""

import json
import os
import stat

import seam
from seam.artifact import ARTIFACT_MODE
import tempfile
import unittest

from seam.canon import digest, dumps, loads
from seam.proof.checkout import CASE_DIR
from seam.runtime import sim_env
from tests.support import (
    CASE_DIGEST,
    DECLINED,
    GOLDEN_BODY,
    REPO,
    RUN_DIGEST,
    body_of,
    child_env,
    run_checkout,
    run_proc,
    run_product_case,
    run_script,
    write_json,
)

import sys


def tearDownModule():
    import time

    if not isinstance(time.time(), float):
        raise AssertionError("parent clock was patched")


def _one_handler_case(namespace, handler, ports=()):
    """A minimal case with one arrival and the named ports.

    Several guard tests need a runnable case that does nothing but call one
    handler, so this is shared rather than repeated inline.

    `ports` must match the ports the handler's runtime registers *exactly*: the
    loader refuses `want - have` as `unknown_port_in_case` and `have - want` as
    `unscripted_port`. A handler that registers nothing therefore needs a case
    declaring nothing, which is the default here.

    Returns the case as a dict, for `write_json`.
    """
    return {
        "format": "seam-case",
        "version": 1,
        "name": namespace,
        "seed": 7,
        "namespace": namespace,
        "clock": {"start_ns": 0, "epoch": "1970-01-01T00:00:00Z"},
        "initial_state": {},
        "arrivals": [{"at_ns": 0, "handler": handler, "body": {}}],
        "ports": {
            name: {"mode": "script", "replies": [{"match": {"$any": True}, "response": {}}]}
            for name in ports
        },
        "stop": {"when": "quiescence"},
    }


class ProcessTest(unittest.TestCase):
    def test_declined_file_is_byte_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            log = os.path.join(directory, "factories.log")
            first = os.path.join(directory, "a.json")
            second = os.path.join(directory, "b.json")
            proc = run_checkout(
                DECLINED,
                "sim-checkout-declined",
                first,
                extra={"CHECKOUT_FACTORY_LOG": log},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout, os.path.abspath(first) + "\n")
            self.assertEqual(proc.stderr, "")
            self.assertFalse(os.path.exists(log))
            self.assertFalse(os.path.exists(first + ".tmp"))
            data = open(first, "rb").read()
            self.assertTrue(data.endswith(b"\n"))
            self.assertEqual(data.count(b"\n"), 1)
            self.assertEqual(stat.S_IMODE(os.stat(first).st_mode), ARTIFACT_MODE)
            art = loads(data.decode("ascii"))
            self.assertEqual(art["digest"], RUN_DIGEST)
            self.assertEqual(art["case_digest"], CASE_DIGEST)
            self.assertEqual(art["package_version"], seam.__version__)
            self.assertEqual(art["status"], "passed")
            self.assertEqual(dumps(body_of(art)), GOLDEN_BODY)
            other = run_checkout(DECLINED, "sim-checkout-declined", second)
            self.assertEqual(other.returncode, 0, other.stderr)
            self.assertEqual(open(first, "rb").read(), open(second, "rb").read())

    def test_pending_and_remind_cases(self):
        with tempfile.TemporaryDirectory() as directory:
            pending = os.path.join(directory, "pending.json")
            proc = run_checkout(
                os.path.join(CASE_DIR, "pending_then_paid.json"),
                "sim-checkout-pending",
                pending,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            art = loads(open(pending, encoding="ascii").read())
            self.assertEqual(art["status"], "passed")
            self.assertEqual(art["final_state"]["order"]["status"], "paid")
            self.assertEqual(art["timers"][0]["outcome"], "cancelled")
            remind = os.path.join(directory, "remind.json")
            proc = run_checkout(
                os.path.join(CASE_DIR, "remind_once.json"),
                "sim-checkout-remind",
                remind,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            art = loads(open(remind, encoding="ascii").read())
            self.assertEqual(art["status"], "passed")
            self.assertEqual(art["timers"][0]["outcome"], "fired")
            self.assertEqual(art["final_state"]["order"]["reminded"], True)

    def test_assertions_move_and_grader(self):
        with tempfile.TemporaryDirectory() as directory:
            moved_path = os.path.join(directory, "moved.json")
            moved = json.loads(open(DECLINED, encoding="utf-8").read())
            moved["assertions"][2]["value"] = "paid"
            write_json(moved_path, moved)
            art_path = os.path.join(directory, "out.json")
            proc = run_checkout(moved_path, "sim-checkout-declined", art_path)
            self.assertEqual(proc.returncode, 1, proc.stderr)
            art = loads(open(art_path, encoding="ascii").read())
            self.assertEqual(art["digest"], RUN_DIGEST)
            self.assertEqual(art["status"], "failed")
            self.assertNotIn("fault", art)
            self.assertFalse(art["assertions"][2]["ok"])
            graded = json.loads(open(DECLINED, encoding="utf-8").read())
            graded["grader"] = "seam.proof.checkout.grade:nope"
            grade_path = os.path.join(directory, "grade.json")
            write_json(grade_path, graded)
            out = os.path.join(directory, "grade-out.json")
            proc = run_checkout(grade_path, "sim-checkout-declined", out)
            self.assertEqual(proc.returncode, 1, proc.stderr)
            art = loads(open(out, encoding="ascii").read())
            self.assertEqual(art["digest"], RUN_DIGEST)
            self.assertEqual(art["grader"], ["no"])
            self.assertNotIn("fault", art)
            leaked = dict(graded)
            leaked["grader"] = "seam.proof.checkout.grade:leak"
            leak_path = os.path.join(directory, "leak.json")
            write_json(leak_path, leaked)
            leak_out = os.path.join(directory, "leak-out.json")
            proc = run_checkout(leak_path, "sim-checkout-declined", leak_out, timeout=5)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            art = loads(open(leak_out, encoding="ascii").read())
            self.assertEqual(art["digest"], RUN_DIGEST)
            self.assertEqual(art["fault"]["code"], "real_io")
            self.assertEqual(art["fault"]["op"], "socket.getaddrinfo")
            self.assertEqual(digest(body_of(art)), RUN_DIGEST)
            self.assertNotEqual(digest(body_of(art, fault=True)), RUN_DIGEST)
            self.assertNotIn("203.0.113.1", proc.stdout + proc.stderr + open(leak_out, encoding="ascii").read())

    def test_replay_hides_the_sentinel_and_mismatch_does_too(self):
        from seam import tape_from_artifact

        with tempfile.TemporaryDirectory() as directory:
            scripted = os.path.join(directory, "scripted.json")
            proc = run_checkout(DECLINED, "sim-checkout-declined", scripted)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            art = loads(open(scripted, encoding="ascii").read())
            tape = tape_from_artifact(art).decode("ascii")
            hidden = (
                '{"format":"seam-tape","version":1,"at_ns":1,"port":"payments",'
                '"request":{"amount":500},"response":{"status":"SEAM_HIDDEN_SENTINEL"}}\n'
            )
            with open(os.path.join(directory, "pay.jsonl"), "w", encoding="ascii") as handle:
                handle.write(tape + hidden)
            case = json.loads(open(DECLINED, encoding="utf-8").read())
            case["name"] = "checkout-replay"
            case["namespace"] = "sim-checkout-replay"
            case["ports"]["payments"] = {
                "mode": "recording",
                "tape": "pay.jsonl",
                "cutoff_ns": 0,
                "policy": "ordered",
            }
            case_path = os.path.join(directory, "replay.json")
            write_json(case_path, case)
            replay_path = os.path.join(directory, "replay-out.json")
            proc = run_checkout(case_path, "sim-checkout-replay", replay_path)
            blob = proc.stdout + proc.stderr + open(replay_path, encoding="ascii").read()
            self.assertNotIn("SEAM_HIDDEN_SENTINEL", blob)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            replay = loads(open(replay_path, encoding="ascii").read())
            self.assertNotEqual(replay["digest"], art["digest"])
            self.assertEqual(replay["final_state"], art["final_state"])
            self.assertEqual(
                [event["body"] for event in replay["events"] if event["kind"] == "deliver"],
                [event["body"] for event in art["events"] if event["kind"] == "deliver"],
            )
            self.assertEqual(
                [(call["request"], call["response"]) for call in replay["port_calls"]],
                [(call["request"], call["response"]) for call in art["port_calls"]],
            )
            self.assertEqual(replay["port_calls"][0]["source"], "recording")

            marked = (
                '{"format":"seam-tape","version":1,"at_ns":0,"port":"payments",'
                '"request":{"amount":500,"marker":"SEAM_HIDDEN_SENTINEL"},'
                '"response":{"status":"declined"}}\n'
            )
            with open(os.path.join(directory, "bad.jsonl"), "w", encoding="ascii") as handle:
                handle.write(marked)
            case["name"] = "checkout-mismatch"
            case["namespace"] = "sim-checkout-mismatch"
            case["ports"]["payments"]["tape"] = "bad.jsonl"
            case["arrivals"][0]["body"] = {"sku": "book", "amount": 999}
            case["assertions"] = [{"op": "fault_is", "code": "tape_mismatch"}]
            bad_case = os.path.join(directory, "mismatch.json")
            write_json(bad_case, case)
            bad_out = os.path.join(directory, "mismatch-out.json")
            proc = run_checkout(bad_case, "sim-checkout-mismatch", bad_out)
            text = proc.stdout + proc.stderr + open(bad_out, encoding="ascii").read()
            self.assertNotIn("SEAM_HIDDEN_SENTINEL", text)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            got = loads(open(bad_out, encoding="ascii").read())
            self.assertEqual(got["fault"]["code"], "tape_mismatch")
            self.assertEqual(got["port_calls"][0]["request"], {"amount": 999})
            self.assertIsNone(got["port_calls"][0]["response"])

    def test_handler_leaks(self):
        expect = {
            "socket": ("real_io", "socket.getaddrinfo"),
            "clock": ("real_clock", "time.time"),
            "uuid": ("unseeded_random", "uuid"),
            "thread": ("thread", "thread"),
            "urandom": ("unseeded_random", "os.urandom"),
            "devrandom": ("unseeded_random", "os.urandom"),
            "subprocess": ("real_io", "subprocess"),
            "datetime": ("real_clock", "datetime.now"),
            "random": ("unseeded_random", "random"),
            # Escapes found by probing the guard, each one a way a run could
            # otherwise report `passed` while depending on the host.
            "random_instance": ("unseeded_random", "random"),
            "ctypes": ("real_io", "ctypes"),
            "perf_counter": ("real_clock", "time.perf_counter"),
            "process_time": ("real_clock", "time.process_time"),
            "file_read": ("file_read", "file.read"),
            "file_write": ("file_write", "file.write"),
            "os_open_read": ("file_read", "file.read"),
            "os_stat": ("file_read", "file.stat"),
            "listdir": ("file_read", "os.listdir"),
            "scandir": ("file_read", "os.scandir"),
        }
        raw = json.loads(open(DECLINED, encoding="utf-8").read())
        with tempfile.TemporaryDirectory() as directory:
            leak_file = os.path.join(directory, "secret.txt")
            with open(leak_file, "w", encoding="ascii") as handle:
                handle.write("secret")
            for kind, (code, op) in expect.items():
                with self.subTest(kind=kind):
                    case = json.loads(json.dumps(raw))
                    case["name"] = "leak-case"
                    case["namespace"] = "sim-leak-case"
                    case["arrivals"] = [
                        {
                            "at_ns": 0,
                            "handler": "message",
                            "body": {"sku": "book", "amount": 500, "leak": kind},
                        }
                    ]
                    case["assertions"] = [{"op": "fault_is", "code": code}]
                    case_path = os.path.join(directory, kind + ".json")
                    out = os.path.join(directory, kind + "-out.json")
                    write_json(case_path, case)
                    proc = run_checkout(
                        case_path,
                        "sim-leak-case",
                        out,
                        extra={"SEAM_LEAK_FILE": leak_file, "SEAM_LEAK_DIR": directory},
                        timeout=5,
                    )
                    self.assertEqual(proc.returncode, 2, proc.stderr)
                    art = loads(open(out, encoding="ascii").read())
                    self.assertEqual(art["fault"]["code"], code)
                    self.assertEqual(art["fault"]["op"], op)
                    self.assertEqual(art["port_calls"], [])
                    self.assertNotIn("should-not-run", proc.stdout + proc.stderr)
                    self.assertNotIn("203.0.113.1", proc.stdout + proc.stderr)
                    # A refused write must not have touched the file.
                    with open(leak_file, encoding="ascii") as handle:
                        self.assertEqual(handle.read(), "secret")

    def test_libc_clock_fails_closed_instead_of_passing(self):
        """The defect this guards: a libc clock read a passing run with a
        digest that changed on every replay. It must fault now, and fault the
        same way twice."""
        raw = json.loads(open(DECLINED, encoding="utf-8").read())
        with tempfile.TemporaryDirectory() as directory:
            case = json.loads(json.dumps(raw))
            case["name"] = "libc-case"
            case["namespace"] = "sim-libc-case"
            case["arrivals"] = [
                {
                    "at_ns": 0,
                    "handler": "message",
                    "body": {"sku": "book", "amount": 500, "leak": "ctypes"},
                }
            ]
            case_path = os.path.join(directory, "libc.json")
            write_json(case_path, case)
            digests = []
            for index in (1, 2):
                out = os.path.join(directory, f"libc-{index}.json")
                proc = run_checkout(case_path, "sim-libc-case", out, timeout=5)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                art = loads(open(out, encoding="ascii").read())
                self.assertEqual(art["status"], "failed")
                self.assertEqual(art["fault"]["code"], "real_io")
                self.assertEqual(art["fault"]["op"], "ctypes")
                digests.append(art["digest"])
            self.assertEqual(digests[0], digests[1])

    def test_handler_cannot_widen_its_own_policy(self):
        """A handler must not be able to grant itself host access.

        Every one of these reached the host filesystem and the run still
        reported `passed`, before the policy was sealed. They are the reason
        `allow_read`, `allow_write` and `trusted()` are refused inside a handler
        and why the audit hook keys off a token rather than a boolean flag.
        """
        attempts = {
            # Each entry is (body, actually_reaches_the_host). An attempt that
            # is refused mid-handler may still leave the run green, because the
            # handler was doing nothing else that mattered. What must never
            # happen is the host value reaching the artifact.
            "allow_read": (
                "from seam.guard import allow_read\n"
                "allow_read(os.environ['SECRET'])\n"
                "ctx.emit('payments', {'leaked': open(os.environ['SECRET']).read().strip()})\n",
                True,
            ),
            "trusted": (
                "from seam.guard import trusted\n"
                "with trusted():\n"
                "    data = open(os.environ['SECRET']).read()\n"
                "ctx.emit('payments', {'leaked': data.strip()})\n",
                True,
            ),
            "read_paths": (
                "from seam.guard import POLICY\n"
                "POLICY.read_paths.add(os.path.realpath(os.environ['SECRET']))\n"
                "ctx.emit('payments', {'leaked': open(os.environ['SECRET']).read().strip()})\n",
                False,
            ),
            "armed_flag": (
                "from seam.guard import POLICY\n"
                "POLICY.armed = False\n"
                "ctx.emit('payments', {'leaked': open(os.environ['SECRET']).read().strip()})\n",
                False,
            ),
            "extra_reads": (
                "from seam.guard import POLICY\n"
                "POLICY.extra_reads.add(os.path.realpath(os.environ['SECRET']))\n"
                "ctx.emit('payments', {'leaked': open(os.environ['SECRET']).read().strip()})\n",
                False,
            ),
            "real_stat": (
                "import os\n"
                "from seam.guard import _real_stat\n"
                "st = _real_stat(os.environ['SECRET'])\n"
                "ctx.emit('payments', {'size': st.st_size})\n",
                True,
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            secret = os.path.join(directory, "secret.txt")
            with open(secret, "w", encoding="ascii") as handle:
                handle.write("HOST-SECRET-VALUE")
            for name, (body, reaches_host) in attempts.items():
                with self.subTest(attempt=name):
                    case = json.loads(open(DECLINED, encoding="utf-8").read())
                    case["name"] = "widen-case"
                    case["namespace"] = "sim-widen-case"
                    case["arrivals"] = [{"at_ns": 0, "handler": "message", "body": {"sku": "book", "amount": 1}}]
                    case["ports"]["payments"]["replies"] = [
                        {"match": {"$any": True}, "response": {"status": "declined"}}
                    ]
                    case["assertions"] = []
                    case_path = os.path.join(directory, name + ".json")
                    out = os.path.join(directory, name + "-out.json")
                    write_json(case_path, case)
                    script = (
                        "import os, sys\n"
                        "from seam import Runtime, main\n"
                        "rt = Runtime(namespace='shop')\n"
                        "rt.port('payments', lambda: (lambda request: {'status': 'declined'}))\n"
                        "rt.port('email', lambda: (lambda request: {'status': 'accepted'}))\n"
                        "def message(ctx, body):\n"
                        # The attempt body is inserted already indented, so it
                        # becomes the body of `message`.
                        + "".join(
                            "    " + line + "\n"
                            for line in body.strip("\n").split("\n")
                        )
                        + "rt.on('message', message)\n"
                        "rt.on('remind', lambda ctx, body: None)\n"
                        "sys.stdout.write('CODE %s\\n' % main(rt))\n"
                    )
                    proc = run_product_case(
                        case_path,
                        "sim-widen-case",
                        out,
                        script,
                        extra={"SECRET": secret},
                    )
                    art = loads(open(out, encoding="ascii").read())
                    blob = dumps(art) + proc.stdout + proc.stderr
                    # The host value must appear nowhere in the artifact or output.
                    self.assertNotIn("HOST-SECRET-VALUE", blob, name)
                    # `main` returns the exit code but the program does not exit
                    # with it, so the artifact is the source of truth here.
                    self.assertIn("CODE 2", proc.stdout, name)
                    if reaches_host:
                        # The attempt itself has to be stopped, not just silent.
                        self.assertEqual(art["status"], "failed", name)
                        self.assertEqual(
                            art["fault"]["code"], "file_access", name
                        )
                    else:
                        # This one only asks for metadata; blocking the stat is
                        # enough, and the run may still finish normally.
                        self.assertNotIn('"leaked"', blob, name)

    def test_hash_seed_pinning_survives_a_real_run(self):
        """A real replay is stable when a handler iterates a set.

        `test_sim_env_pins_the_hash_seed` checks that `sim_env` builds an
        environment containing `PYTHONHASHSEED`, which is a claim about a dict.
        This is the claim about a run: a handler that puts `list(set(...))` into
        a port request, launched through `child_env` the way the rest of the
        suite launches children, has to produce the same digest every time.

        Before `child_env` pinned the seed, this failed -- five runs, five
        digests, every one reporting `status: passed`.
        """
        import os
        import tempfile

        from tests.support import child_env, run_script, write_json

        digests = set()
        statuses = set()
        for _ in range(5):
            with tempfile.TemporaryDirectory() as directory:
                case_path = os.path.join(directory, "case.json")
                artifact = os.path.join(directory, "out.json")
                write_json(
                    case_path,
                    {
                        "format": "seam-case",
                        "version": 1,
                        "name": "hash-seed",
                        "seed": 1842,
                        "namespace": "sim-hash-seed",
                        "clock": {"start_ns": 0, "epoch": "1970-01-01T00:00:00Z"},
                        "initial_state": {},
                        "arrivals": [{"at_ns": 0, "handler": "go", "body": {}}],
                        "ports": {"p": {"mode": "script", "replies": [
                            {"match": {"$any": True}, "response": {}}]}},
                        "stop": {"when": "quiescence"},
                    },
                )
                env = child_env(
                    SEAM_SIM="1",
                    SEAM_NAMESPACE="sim-hash-seed",
                    SEAM_CASE=case_path,
                    SEAM_ARTIFACT=artifact,
                )
                program = (
                    "import sys\n"
                    "from seam import Runtime, main\n"
                    "rt = Runtime(namespace='shop')\n"
                    "rt.port('p', lambda: (lambda request: {}))\n"
                    "def go(ctx, body):\n"
                    # Deliberately unordered: str hashing is salt-randomised, so
                    # set iteration order differs per process unless the seed is
                    # pinned for the child.
                    "    items = list(set(['a','bb','ccc','d','ee','fff',"
                    "'g','hh','iii','jjj','kkkk','lllll']))\n"
                    "    ctx.emit('p', {'items': items})\n"
                    "rt.on('go', go)\n"
                    "sys.stdout.write('CODE %s\\n' % main(rt))\n"
                )
                proc = run_script(program, env, timeout=20)
                self.assertIn("CODE 0", proc.stdout, proc.stderr)
                art = loads(open(artifact, encoding="ascii").read())
                self.assertEqual(art["status"], "passed", dumps(art)[:300])
                digests.add(art["digest"])
                statuses.add(art["status"])

        self.assertEqual(
            len(digests),
            1,
            f"digest moved across runs with the seed pinned: {sorted(digests)}",
        )
        self.assertEqual(statuses, {"passed"})

    def test_child_env_pins_the_hash_seed(self):
        """`child_env` must not inherit the parent's hash seed.

        The parent process's own seed is fixed at startup and cannot be changed,
        so the only way to give a child a stable one is to set it in the
        environment that launches it. That makes this the single point where
        every test child gets the pin.
        """
        from tests.support import child_env

        self.assertEqual(child_env()["PYTHONHASHSEED"], "0")
        # An explicit value still wins, so a test can choose a different seed.
        self.assertEqual(child_env(PYTHONHASHSEED="7")["PYTHONHASHSEED"], "7")
        # And it does not leak a SEAM_ variable while it is at it.
        self.assertNotIn("SEAM_CASE", child_env())

    def test_sim_env_pins_the_hash_seed(self):
        """Set iteration order must not change the digest.

        String hashing is salted per interpreter, so `list({"a", "b"})` came out
        in a different order in every process and the digest changed with it,
        while the run still reported `passed`. The salt is fixed at start-up, so
        `sim_env` sets it for the child rather than the process trying to fix it
        from inside.
        """
        with tempfile.TemporaryDirectory() as directory:
            env = sim_env("case.json", "sim-shop", "out.json", PYTHONHASHSEED=None)
            self.assertEqual(env["SEAM_SIM"], "1")
            self.assertEqual(env["SEAM_CASE"], "case.json")
            self.assertEqual(env["SEAM_NAMESPACE"], "sim-shop")
            self.assertEqual(env["SEAM_ARTIFACT"], "out.json")
            # A cleared key falls back to the pinned default.
            self.assertEqual(env["PYTHONHASHSEED"], "0")
            # An explicit seed is respected rather than overwritten.
            self.assertEqual(
                sim_env("c", "n", PYTHONHASHSEED="7")["PYTHONHASHSEED"], "7"
            )
            # Other overrides pass through, and None removes the key.
            self.assertEqual(sim_env("c", "n", SEED_PIN="x")["SEED_PIN"], "x")
            self.assertNotIn("SEED_PIN", sim_env("c", "n", SEED_PIN=None))

            case = json.loads(open(DECLINED, encoding="utf-8").read())
            case["name"] = "set-case"
            case["namespace"] = "sim-set-case"
            case["arrivals"] = [{"at_ns": 0, "handler": "message", "body": {"sku": "book", "amount": 1}}]
            case["ports"]["payments"]["replies"] = [
                {"match": {"$any": True}, "response": {"status": "declined"}}
            ]
            case["assertions"] = []
            case_path = os.path.join(directory, "case.json")
            write_json(case_path, case)
            program = (
                "import sys\n"
                "from seam import Runtime, main\n"
                "rt = Runtime(namespace='shop')\n"
                "rt.port('payments', lambda: (lambda request: {'status': 'declined'}))\n"
                "rt.port('email', lambda: (lambda request: {'status': 'accepted'}))\n"
                "def message(ctx, body):\n"
                "    ctx.emit('payments', {'order': list({'charlie','echo','foxtrot','alpha'})})\n"
                "rt.on('message', message)\n"
                "rt.on('remind', lambda ctx, body: None)\n"
                "main(rt)\n"
            )
            digests = []
            for index in (1, 2, 3):
                out = os.path.join(directory, f"out{index}.json")
                env = sim_env(case_path, "sim-set-case", out, PYTHONPATH=REPO)
                run_proc([sys.executable, "-c", program], env, timeout=30)
                digests.append(loads(open(out, encoding="ascii").read())["digest"])
            self.assertEqual(len(set(digests)), 1, digests)

    def test_handler_cannot_reach_a_shell_by_forking(self):
        """The widest hole in the guard, and it reported `passed`.

        `os.fork` fires an audit event but nothing inspected it, so a handler
        could fork a child, `execv` a real binary, and read the host. The child
        is a fresh process with none of the guards installed, so this was a
        complete escape rather than a leak.

        Blocking `os.fork` is the load-bearing part: `os.execv` fires only
        CPython's bare `exec` event, which cannot be blocked because ordinary
        `exec()` of a Python object raises it too.
        """
        with tempfile.TemporaryDirectory() as directory:
            secret = os.path.join(directory, "secret.txt")
            with open(secret, "w", encoding="ascii") as handle:
                handle.write("HOSTSECRET-CANARY")
            for attempt in ("fork_exec", "fork_only", "os_times"):
                with self.subTest(attempt=attempt):
                    case = json.loads(open(DECLINED, encoding="utf-8").read())
                    case["name"] = "fork-case"
                    case["namespace"] = "sim-fork-case"
                    case["arrivals"] = [
                        {"at_ns": 0, "handler": "message", "body": {"sku": "book", "amount": 1}}
                    ]
                    case["ports"]["payments"]["replies"] = [
                        {"match": {"$any": True}, "response": {"status": "declined"}}
                    ]
                    case["assertions"] = []
                    case_path = os.path.join(directory, attempt + ".json")
                    write_json(case_path, case)
                    out = os.path.join(directory, attempt + "-out.json")
                    if attempt == "fork_exec":
                        body = (
                            "import os\n"
                            "r_fd, w_fd = os.pipe()\n"
                            "pid = os.fork()\n"
                            "if pid == 0:\n"
                            "    os.close(r_fd)\n"
                            "    os.dup2(w_fd, 1)\n"
                            "    os.close(w_fd)\n"
                            "    os.execv('/bin/cat', ['/bin/cat', os.environ['SECRET']])\n"
                            "os.close(w_fd)\n"
                            "chunks = []\n"
                            "while True:\n"
                            "    block = os.read(r_fd, 256)\n"
                            "    if not block:\n"
                            "        break\n"
                            "    chunks.append(block)\n"
                            "os.close(r_fd)\n"
                            "os.waitpid(pid, 0)\n"
                            "ctx.emit('payments', {'leaked': b''.join(chunks).decode('utf-8','replace')})\n"
                        )
                        want = "real_io"
                    elif attempt == "fork_only":
                        body = (
                            "import os\n"
                            "pid = os.fork()\n"
                            "if pid == 0:\n"
                            "    os._exit(0)\n"
                            "os.waitpid(pid, 0)\n"
                            "ctx.emit('payments', {'forked': True})\n"
                        )
                        want = "real_io"
                    else:
                        body = (
                            "import os\n"
                            "ctx.emit('payments', {'t': str(os.times())})\n"
                        )
                        want = "real_clock"
                    script = (
                        "import os, sys\n"
                        "from seam import Runtime, main\n"
                        "rt = Runtime(namespace='shop')\n"
                        "rt.port('payments', lambda: (lambda request: {'status': 'declined'}))\n"
                        "rt.port('email', lambda: (lambda request: {'status': 'accepted'}))\n"
                        "def message(ctx, body):\n"
                        + "".join("    " + line + "\n" for line in body.strip("\n").split("\n"))
                        + "rt.on('message', message)\n"
                        "rt.on('remind', lambda ctx, body: None)\n"
                        "main(rt)\n"
                    )
                    proc = run_product_case(
                        case_path, "sim-fork-case", out, script, extra={"SECRET": secret}
                    )
                    art = loads(open(out, encoding="ascii").read())
                    blob = dumps(art) + proc.stdout + proc.stderr
                    self.assertNotIn("HOSTSECRET-CANARY", blob, attempt)
                    self.assertEqual(art["fault"]["code"], want, attempt)
                    self.assertEqual(art["status"], "failed", attempt)

    def test_lazy_import_of_a_cold_subpackage_works(self):
        """A lazy `import` inside a handler must work, including for a stdlib
        subpackage the interpreter has not touched yet.

        The import system lists a directory to find a module in it. Refusing
        directory listing outright made this fail, which contradicted the README
        and broke `importlib.util.find_spec` as well.
        """
        with tempfile.TemporaryDirectory(dir=os.path.dirname(REPO)) as directory:
            case = json.loads(open(DECLINED, encoding="utf-8").read())
            case["name"] = "lazy-case"
            case["namespace"] = "sim-lazy-case"
            case["arrivals"] = [{"at_ns": 0, "handler": "message", "body": {"sku": "book", "amount": 1}}]
            case["ports"]["payments"]["replies"] = [
                {"match": {"$any": True}, "response": {"status": "declined"}}
            ]
            case["assertions"] = []
            case_path = os.path.join(directory, "case.json")
            write_json(case_path, case)
            out = os.path.join(directory, "out.json")
            script = (
                "import os, sys\n"
                "from seam import Runtime, main\n"
                "rt = Runtime(namespace='shop')\n"
                "rt.port('payments', lambda: (lambda request: {'status': 'declined'}))\n"
                "rt.port('email', lambda: (lambda request: {'status': 'accepted'}))\n"
                "def message(ctx, body):\n"
                # A cold stdlib subpackage, and the import system's own directory
                # listing, both of which used to fault.
                "    import xml.sax\n"
                "    import importlib.util\n"
                "    spec = importlib.util.find_spec('xml.sax')\n"
                "    ctx.emit('payments', {'found': spec is not None})\n"
                "rt.on('message', message)\n"
                "rt.on('remind', lambda ctx, body: None)\n"
                "sys.stdout.write('CODE %s\\n' % main(rt))\n"
            )
            proc = run_product_case(case_path, "sim-lazy-case", out, script)
            self.assertIn("CODE 0", proc.stdout, proc.stderr)
            art = loads(open(out, encoding="ascii").read())
            self.assertEqual(art["status"], "passed", dumps(art)[:400])
            self.assertEqual(art["port_calls"][0]["request"], {"found": True})

    def test_lazy_import_skips_temp_paths_after_cache_invalidation(self):
        """Cold imports skip excluded paths even when FileFinder must refresh."""
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory(
            dir=os.path.dirname(REPO)
        ) as code_directory:
            nested = os.path.join(directory, "nested")
            os.mkdir(nested)
            link = os.path.join(code_directory, "temp-link")
            os.symlink(nested, link)
            variants = (
                (tempfile.gettempdir(), REPO),
                (nested, REPO),
                ("", directory),
                (".", directory),
                ("nested", directory),
                (link, REPO),
            )
            case_path = os.path.join(directory, "case.json")
            write_json(case_path, _one_handler_case("sim-cold-import", "go"))
            for index, (path, cwd) in enumerate(variants):
                with self.subTest(path=path):
                    out = os.path.join(directory, f"out-{index}.json")
                    script = (
                        "import os, sys\n"
                        "from importlib import invalidate_caches\n"
                        f"os.chdir({cwd!r})\n"
                        f"sys.path.insert(0, {path!r})\n"
                        f"sys.path.append({path!r})\n"
                        "paths = sys.path\n"
                        "before = list(sys.path)\n"
                        "from seam import Runtime, main\n"
                        "rt = Runtime(namespace='cold-import')\n"
                        "def go(ctx, body):\n"
                        "    invalidate_caches()\n"
                        "    import xml.sax\n"
                        "    import importlib.util\n"
                        "    ctx.patch('found', importlib.util.find_spec('xml.sax') is not None)\n"
                        "    ctx.patch('unchanged', sys.path == before)\n"
                        "    ctx.patch('same_list', paths is sys.path)\n"
                        "rt.on('go', go)\n"
                        "print('CODE', main(rt))\n"
                    )
                    proc = run_product_case(case_path, "sim-cold-import", out, script)
                    with open(out, encoding="ascii") as handle:
                        art = loads(handle.read())
                    self.assertIn("CODE 0", proc.stdout, dumps(art))
                    self.assertEqual(art["status"], "passed", dumps(art))
                    self.assertEqual(
                        art["final_state"],
                        {"found": True, "unchanged": True, "same_list": True},
                    )
                    self.assertIsNone(art.get("fs_reads"))

    def test_namespace_import_refresh_keeps_dynamic_code_paths(self):
        """Namespace package refreshes use the same guarded path search."""
        with tempfile.TemporaryDirectory(dir=os.path.dirname(REPO)) as directory:
            for name, module, value in (("first", "one", 1), ("second", "two", 2)):
                package = os.path.join(directory, name, "seam_namespace_canary")
                os.makedirs(package)
                with open(os.path.join(package, module + ".py"), "w", encoding="ascii") as handle:
                    handle.write(f"VALUE = {value}\n")
            first, second = (os.path.join(directory, name) for name in ("first", "second"))
            case_path = os.path.join(directory, "case.json")
            out = os.path.join(directory, "out.json")
            write_json(case_path, _one_handler_case("sim-namespace-import", "go"))
            script = (
                "import sys, tempfile\n"
                "from importlib import invalidate_caches\n"
                f"sys.path[:0] = [tempfile.gettempdir(), {first!r}, {second!r}]\n"
                "from seam import Runtime, main\n"
                "rt = Runtime(namespace='namespace-import')\n"
                "def go(ctx, body):\n"
                f"    sys.path.remove({second!r})\n"
                "    invalidate_caches()\n"
                "    import seam_namespace_canary.one\n"
                f"    sys.path.append({second!r})\n"
                "    invalidate_caches()\n"
                "    import seam_namespace_canary.two\n"
                "    ctx.set_state({'one': seam_namespace_canary.one.VALUE,\n"
                "                   'two': seam_namespace_canary.two.VALUE})\n"
                "rt.on('go', go)\n"
                "print('CODE', main(rt))\n"
            )
            proc = run_product_case(case_path, "sim-namespace-import", out, script)
            with open(out, encoding="ascii") as handle:
                art = loads(handle.read())
            self.assertIn("CODE 0", proc.stdout, dumps(art))
            self.assertEqual(art["final_state"], {"one": 1, "two": 2})
            self.assertIsNone(art.get("fs_reads"))

    def test_custom_import_finders_are_preserved(self):
        """Only the default filesystem finder changes in simulation."""
        with tempfile.TemporaryDirectory() as directory:
            case_path = os.path.join(directory, "case.json")
            out = os.path.join(directory, "out.json")
            write_json(case_path, _one_handler_case("sim-custom-import", "go"))
            script = (
                "import sys\n"
                "from importlib.machinery import ModuleSpec\n"
                "class Finder:\n"
                "    def find_spec(self, fullname, path=None, target=None):\n"
                "        if fullname == 'seam_virtual_canary':\n"
                "            return ModuleSpec(fullname, self)\n"
                "    def create_module(self, spec):\n"
                "        return None\n"
                "    def exec_module(self, module):\n"
                "        module.VALUE = 7\n"
                "finder = Finder()\n"
                "sys.meta_path.insert(0, finder)\n"
                "meta_path = sys.meta_path\n"
                "from seam import Runtime, main\n"
                "rt = Runtime(namespace='custom-import')\n"
                "def go(ctx, body):\n"
                "    import seam_virtual_canary\n"
                "    assert sys.meta_path is meta_path and sys.meta_path[0] is finder\n"
                "    ctx.set_state({'value': seam_virtual_canary.VALUE})\n"
                "rt.on('go', go)\n"
                "print('CODE', main(rt))\n"
            )
            proc = run_product_case(case_path, "sim-custom-import", out, script)
            with open(out, encoding="ascii") as handle:
                art = loads(handle.read())
            self.assertIn("CODE 0", proc.stdout, dumps(art))
            self.assertEqual(art["final_state"], {"value": 7})

    def test_temp_import_path_filter_does_not_grant_filesystem_access(self):
        """Skipping import paths must not allow temp reads, stats, or listings."""
        with tempfile.TemporaryDirectory() as directory:
            secret = os.path.join(directory, "seam_temp_canary.py")
            with open(secret, "w", encoding="ascii") as handle:
                handle.write("CANARY = 'HOSTSECRET-TEMP-CANARY'\n")
            case_path = os.path.join(directory, "case.json")
            write_json(case_path, _one_handler_case("sim-temp-access", "go"))
            attempts = (
                f"os.listdir({directory!r})",
                f"os.scandir({directory!r})",
                f"os.stat({secret!r})",
                f"open({secret!r}).read()",
                f"sys.path.insert(0, {directory!r}); import seam_temp_canary",
            )
            for index, attempt in enumerate(attempts):
                with self.subTest(attempt=attempt):
                    out = os.path.join(directory, f"out-{index}.json")
                    script = (
                        "import os, sys\n"
                        f"sys.path.insert(0, {directory!r})\n"
                        "from seam import Runtime, main\n"
                        "rt = Runtime(namespace='temp-access')\n"
                        "def go(ctx, body):\n"
                        f"    {attempt}\n"
                        "rt.on('go', go)\n"
                        "print('CODE', main(rt))\n"
                    )
                    proc = run_product_case(case_path, "sim-temp-access", out, script)
                    self.assertIn("CODE 2", proc.stdout, proc.stderr)
                    with open(out, encoding="ascii") as handle:
                        art = loads(handle.read())
                    if "import seam_temp_canary" in attempt:
                        self.assertEqual(art["fault"]["code"], "handler_error")
                        self.assertEqual(art["fault"]["exc_type"], "ModuleNotFoundError")
                    else:
                        self.assertEqual(art["fault"]["code"], "file_read")
                    self.assertNotIn("HOSTSECRET-TEMP-CANARY", dumps(art) + proc.stdout + proc.stderr)

    def test_live_lazy_import_keeps_temp_search_paths(self):
        """Live mode leaves import paths and bytecode policy untouched."""
        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, "seam_live_canary.py"), "w", encoding="ascii") as handle:
                handle.write("VALUE = 'live-import-ok'\n")
            script = (
                "import sys\n"
                f"sys.path.insert(0, {directory!r})\n"
                "before = list(sys.path)\n"
                "bytecode = sys.dont_write_bytecode\n"
                "from seam import Runtime\n"
                "rt = Runtime(namespace='live-import')\n"
                "def go(ctx, body):\n"
                "    import seam_live_canary\n"
                "    print(seam_live_canary.VALUE)\n"
                "rt.on('go', go)\n"
                "rt.start_live()\n"
                "rt.deliver('go', {})\n"
                "assert sys.path == before\n"
                "assert sys.dont_write_bytecode == bytecode\n"
            )
            proc = run_script(script, child_env())
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout, "live-import-ok\n")

    def test_patched_stat_returns_a_real_result_and_full_signature(self):
        """`_check_stat` must behave like `os.stat`.

        It used to return None, so `os.path.exists()` would answer True for a
        path that does not exist, and it took only `(path)`, so `pathlib`,
        `shutil` and `linecache` died with a `TypeError` instead of a `file_read`.
        """
        script = (
            "import os, sys, linecache\n"
            "from seam import Runtime, main\n"
            "rt = Runtime(namespace='shop')\n"
            "rt.port('payments', lambda: (lambda request: {'status': 'declined'}))\n"
            "rt.port('email', lambda: (lambda request: {'status': 'accepted'}))\n"
            "def message(ctx, body):\n"
            "    path = os.environ['NOTE']\n"
            "    st = os.stat(path, follow_symlinks=False)\n"
            "    ctx.emit('payments', {'size': st.st_size, 'exists': os.path.exists(path)})\n"
            "rt.on('message', message)\n"
            "rt.on('remind', lambda ctx, body: None)\n"
            "main(rt)\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            note = os.path.join(directory, "note.txt")
            with open(note, "w", encoding="ascii") as handle:
                handle.write("hello")
            case = json.loads(open(DECLINED, encoding="utf-8").read())
            case["name"] = "stat-case"
            case["namespace"] = "sim-stat-case"
            case["arrivals"] = [{"at_ns": 0, "handler": "message", "body": {"sku": "book", "amount": 1}}]
            case["ports"]["payments"]["replies"] = [
                {"match": {"$any": True}, "response": {"status": "declined"}}
            ]
            case["assertions"] = []
            case_path = os.path.join(directory, "case.json")
            write_json(case_path, case)
            out = os.path.join(directory, "out.json")
            proc = run_product_case(
                case_path, "sim-stat-case", out, script, extra={"NOTE": note}
            )
            art = loads(open(out, encoding="ascii").read())
            self.assertEqual(art["fault"]["code"], "file_read", dumps(art)[:300])
            self.assertEqual(art["port_calls"], [])

    def test_handler_can_import_the_project_its_own_modules(self):
        """A handler importing its own modules is ordinary product code.

        It must work. The first cut of this policy refused it, because only the
        interpreter's directories were allowlisted for reads, so a handler doing
        `import mylib` faulted on the module file. Code directories come from
        `sys.path` now, which is where a product's modules actually live.
        """
        with tempfile.TemporaryDirectory(dir=os.path.dirname(REPO)) as directory:
            app = os.path.join(directory, "app")
            os.makedirs(app)
            with open(os.path.join(app, "productmod.py"), "w", encoding="ascii") as handle:
                handle.write("def payload():\n    return {'from': 'product'}\n")
            case = json.loads(open(DECLINED, encoding="utf-8").read())
            case["name"] = "import-case"
            case["namespace"] = "sim-import-case"
            case["arrivals"] = [{"at_ns": 0, "handler": "message", "body": {"sku": "book", "amount": 1}}]
            case["ports"]["payments"]["replies"] = [
                {"match": {"$any": True}, "response": {"status": "declined"}}
            ]
            case["assertions"] = [
                {"op": "port_called", "port": "payments", "times": 1, "match": {"from": "product"}}
            ]
            case_path = os.path.join(directory, "case.json")
            write_json(case_path, case)
            out = os.path.join(directory, "out.json")
            script = (
                "import os, sys\n"
                "sys.path.insert(0, os.environ['APP'])\n"
                "from seam import Runtime, main\n"
                "rt = Runtime(namespace='shop')\n"
                "rt.port('payments', lambda: (lambda request: {'status': 'declined'}))\n"
                "rt.port('email', lambda: (lambda request: {'status': 'accepted'}))\n"
                "def message(ctx, body):\n"
                "    import productmod\n"
                "    ctx.emit('payments', productmod.payload())\n"
                "rt.on('message', message)\n"
                "rt.on('remind', lambda ctx, body: None)\n"
                "sys.stdout.write('CODE %s\\n' % main(rt))\n"
            )
            proc = run_product_case(
                case_path, "sim-import-case", out, script, extra={"APP": app}
            )
            art = loads(open(out, encoding="ascii").read())
            self.assertIn("CODE 0", proc.stdout, proc.stderr)
            self.assertEqual(art["status"], "passed", dumps(art)[:400])
            self.assertEqual(art["port_calls"][0]["request"], {"from": "product"})
            # The module read is code, so it is not recorded as host state.
            self.assertIsNone(art.get("fs_reads"))

    def test_system_temp_is_not_allowlisted_for_code(self):
        """A harness that writes its program into a temp directory would, through
        `sys.path[0]`, allowlist every file in the system temp area. That is not
        a code directory, so it is removed."""
        with tempfile.TemporaryDirectory() as directory:
            app = os.path.join(directory, "app")
            os.makedirs(app)
            case = json.loads(open(DECLINED, encoding="utf-8").read())
            case["name"] = "temp-case"
            case["namespace"] = "sim-temp-case"
            case["arrivals"] = [{"at_ns": 0, "handler": "message", "body": {"sku": "book", "amount": 1}}]
            case["ports"]["payments"]["replies"] = [
                {"match": {"$any": True}, "response": {"status": "declined"}}
            ]
            case["assertions"] = [{"op": "fault_is", "code": "file_read"}]
            case_path = os.path.join(directory, "case.json")
            write_json(case_path, case)
            out = os.path.join(directory, "out.json")
            script = (
                "import os, sys\n"
                "sys.path.insert(0, os.environ['APP'])\n"
                "from seam import Runtime, main\n"
                "rt = Runtime(namespace='shop')\n"
                "rt.port('payments', lambda: (lambda request: {'status': 'declined'}))\n"
                "rt.port('email', lambda: (lambda request: {'status': 'accepted'}))\n"
                "def message(ctx, body):\n"
                "    ctx.emit('payments', {'line': open(os.environ['SECRET']).readline().strip()})\n"
                "rt.on('message', message)\n"
                "rt.on('remind', lambda ctx, body: None)\n"
                "main(rt)\n"
            )
            secret = os.path.join(directory, "secret.txt")
            with open(secret, "w", encoding="ascii") as handle:
                handle.write("HOST-SECRET-VALUE\n")
            proc = run_product_case(
                case_path,
                "sim-temp-case",
                out,
                script,
                extra={"APP": app, "SECRET": secret},
            )
            art = loads(open(out, encoding="ascii").read())
            blob = dumps(art) + proc.stdout + proc.stderr
            self.assertNotIn("HOST-SECRET-VALUE", blob)
            self.assertEqual(art["fault"]["code"], "file_read")

    def test_a_handler_cannot_reach_the_host_through_a_pycache_component(self):
        """End to end: read, overwrite and delete through `<dir>/__pycache__/..`.

        Each of these reported `passed` while the victim file was read,
        overwritten or removed.
        """
        victim_value = "TOPSECRET-VALUE-12345678"
        # Each predicate returns True when the victim was left *intact*, which
        # is the outcome a refused handler must produce. For `read` the file is
        # never the thing that changes; what must not happen is the secret
        # reaching the artifact, and that is asserted below via status/fault.
        for action, intact in (
            ("read", lambda v: open(v, encoding="ascii").read() == victim_value),
            ("write", lambda v: open(v, encoding="ascii").read() == victim_value),
            ("delete", lambda v: os.path.exists(v)),
        ):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as directory:
                victim = os.path.join(directory, "secret.txt")
                with open(victim, "w", encoding="ascii") as handle:
                    handle.write(victim_value)
                os.makedirs(os.path.join(directory, "__pycache__"), exist_ok=True)
                poison = os.path.join(directory, "__pycache__", "..", "secret.txt")

                case = os.path.join(directory, "case.json")
                with open(case, "w", encoding="ascii") as handle:
                    json.dump(
                        {
                            "format": "seam-case",
                            "version": 1,
                            "name": "poison",
                            "seed": 7,
                            "namespace": "sim-poison",
                            "clock": {"start_ns": 0, "epoch": "1970-01-01T00:00:00Z"},
                            "initial_state": {},
                            "arrivals": [{"at_ns": 0, "handler": "go", "body": {}}],
                            "ports": {},
                            "stop": {"when": "quiescence"},
                        },
                        handle,
                    )
                out = os.path.join(directory, "out.json")
                script = (
                    "import os, sys\n"
                    "from seam import Runtime, main\n"
                    "rt = Runtime(namespace='shop')\n"
                    f"POISON = {poison!r}\n"
                    "def go(ctx, body):\n"
                    "    if body.get('action') == 'read':\n"
                    "        ctx.state['v'] = open(POISON, encoding='ascii').read()\n"
                    "    elif body.get('action') == 'write':\n"
                    "        open(POISON, 'w', encoding='ascii').write('CLOBBERED')\n"
                    "    else:\n"
                    "        os.remove(POISON)\n"
                    "rt.on('go', go)\n"
                    "main(rt)\n"
                )
                case_body = json.load(open(case, encoding="ascii"))
                case_body["arrivals"][0]["body"] = {"action": action}
                with open(case, "w", encoding="ascii") as handle:
                    json.dump(case_body, handle)

                proc = run_script(
                    script,
                    child_env(
                        SEAM_SIM="1",
                        SEAM_NAMESPACE="sim-poison",
                        SEAM_CASE=case,
                        SEAM_ARTIFACT=out,
                    ),
                    timeout=20,
                )
                art = loads(open(out, encoding="ascii").read())
                self.assertNotEqual(
                    art["status"],
                    "passed",
                    f"{action} through __pycache__/.. still reports passed: {proc.stderr}",
                )
                self.assertIn(
                    (art.get("fault") or {}).get("code"),
                    ("file_read", "file_write", "file_access"),
                    f"{action}: wrong fault {art.get('fault')}",
                )
                self.assertTrue(
                    intact(victim),
                    f"{action}: the victim file was modified even though the run faulted",
                )
                # And the secret never reached the artifact.
                self.assertNotIn(
                    "TOPSECRET", dumps(art), f"{action}: the secret reached the artifact"
                )

    def test_systemrandom_and_secrets_randbits_are_blocked(self):
        """`SystemRandom` and `secrets.randbits` must not read host entropy.

        `SystemRandom` subclasses `Random` but overrides `random`,
        `getrandbits`, `randbytes` and `seed` with its own implementations, so
        patching `Random` alone left it reading `_urandom` and the run still
        reported `passed`. `secrets.randbits` is `SystemRandom().getrandbits(k)`,
        so blocking only the token helpers left the most obvious way to read OS
        entropy open.

        Both produced a *different digest on every replay* before this was
        fixed, which is the single failure this library exists to prevent.
        """
        sources = [
            "random.SystemRandom().getrandbits(64)",
            "random.SystemRandom().random()",
            "random.SystemRandom().randbytes(8).hex()",
            "random.Random(1).getrandbits(64)",
            "secrets.randbits(64)",
            "secrets.randbelow(64)",
            "secrets.token_bytes(8).hex()",
            "os.urandom(8).hex()",
            "uuid.uuid4().hex",
        ]
        for source in sources:
            with self.subTest(source=source):
                with tempfile.TemporaryDirectory() as directory:
                    out = os.path.join(directory, "out.json")
                    case = os.path.join(directory, "case.json")
                    write_json(case, _one_handler_case("sim-entropy", "go"))
                    script = (
                        "import os, random, secrets, sys, uuid\n"
                        "from seam import Runtime, main\n"
                        "rt = Runtime(namespace='shop')\n"
                        "def go(ctx, body):\n"
                        f"    ctx.state['v'] = repr({source})\n"
                        "rt.on('go', go)\n"
                        "sys.stdout.write('CODE %s\\n' % main(rt))\n"
                    )
                    proc = run_script(
                        script,
                        child_env(
                            SEAM_SIM="1",
                            SEAM_NAMESPACE="sim-entropy",
                            SEAM_CASE=case,
                            SEAM_ARTIFACT=out,
                        ),
                        timeout=20,
                    )
                    self.assertIn("CODE", proc.stdout, proc.stderr)
                    art = loads(open(out, encoding="ascii").read())
                    self.assertEqual(
                        (art.get("fault") or {}).get("code"),
                        "unseeded_random",
                        f"{source} was not blocked: {art.get('status')} {art.get('fault')}",
                    )
                    self.assertNotEqual(art["status"], "passed", source)

    def test_entropy_sources_are_stable_across_replays(self):
        """The property itself: the same run must give the same digest.

        The test above asserts each source is refused. This one asserts the
        guarantee that motivated refusing them, so a future change that let one
        through without an obvious fault code still gets caught.
        """
        digests = set()
        for index in range(3):
            with tempfile.TemporaryDirectory() as directory:
                out = os.path.join(directory, "out.json")
                case = os.path.join(directory, "case.json")
                write_json(case, _one_handler_case("sim-entropy-stable", "go"))
                script = (
                    "import secrets, sys\n"
                    "from seam import Runtime, main\n"
                    "rt = Runtime(namespace='shop')\n"
                    "def go(ctx, body):\n"
                    "    ctx.state['v'] = repr(secrets.randbits(64))\n"
                    "rt.on('go', go)\n"
                    "sys.stdout.write('CODE %s\\n' % main(rt))\n"
                )
                run_script(
                    script,
                    child_env(
                        SEAM_SIM="1",
                        SEAM_NAMESPACE="sim-entropy-stable",
                        SEAM_CASE=case,
                        SEAM_ARTIFACT=out,
                    ),
                    timeout=20,
                )
                art = loads(open(out, encoding="ascii").read())
                digests.add(art["digest"])
        self.assertEqual(len(digests), 1, f"digest moved across replays: {sorted(digests)}")

    def test_handler_cannot_widen_the_policy_by_assigning_to_it(self):
        """The policy's public surface must not be a way to widen it.

        `Policy` exposed `read_paths`, `write_paths`, `sealed`, `trusted` and
        `_in_handler` as plain writable attributes on a module-level singleton.
        Sealing `allow_read` therefore guarded nothing: a handler could assign to
        the state directly. `POLICY.trusted = object()` was the worst of these,
        because the audit hook tested `trusted is not None`, so it skipped every
        filesystem check *and* recorded nothing in `fs_reads`.

        Every realpath below is computed at wiring time, before the run. An
        earlier version of this test called `os.path.realpath` inside the
        handler, which stats every component and dies of `file_read` there, so
        the attempts passed for the wrong reason and proved nothing.
        """
        with tempfile.TemporaryDirectory() as directory:
            secret = os.path.join(directory, "secret.txt")
            with open(secret, "w", encoding="ascii") as handle:
                handle.write("TOPSECRET-VALUE-12345678")
            # Resolved out here, in the parent, where stat is not guarded.
            real = os.path.realpath(secret)

            attempts = [
                f"POLICY.read_paths.add({real!r})",
                f"POLICY.write_paths.add({real!r})",
                "POLICY.sealed = False",
                "POLICY._in_handler = False",
                "POLICY.trusted = object()",
                f"POLICY._read.add({real!r})",
            ]
            for attempt in attempts:
                with self.subTest(attempt=attempt):
                    out = os.path.join(directory, "out.json")
                    case = os.path.join(directory, "case.json")
                    write_json(
                        case,
                        {
                            "format": "seam-case",
                            "version": 1,
                            "name": "policy",
                            "seed": 7,
                            "namespace": "sim-policy",
                            "clock": {"start_ns": 0, "epoch": "1970-01-01T00:00:00Z"},
                            "initial_state": {},
                            "arrivals": [{"at_ns": 0, "handler": "go", "body": {}}],
                            "ports": {"p": {"mode": "script", "replies": [
                                {"match": {"$any": True}, "response": {}}]}},
                            "stop": {"when": "quiescence"},
                        },
                    )
                    script = (
                        "import sys\n"
                        "from seam import Runtime, main\n"
                        "from seam.guard import POLICY\n"
                        "rt = Runtime(namespace='shop')\n"
                        "rt.port('p', lambda: (lambda request: {}))\n"
                        "def go(ctx, body):\n"
                        f"    {attempt}\n"
                        f"    ctx.emit('p', {{'v': open({real!r}, encoding='ascii').read()}})\n"
                        "rt.on('go', go)\n"
                        "sys.stdout.write('CODE %s\\n' % main(rt))\n"
                    )
                    run_script(
                        script,
                        child_env(
                            SEAM_SIM="1",
                            SEAM_NAMESPACE="sim-policy",
                            SEAM_CASE=case,
                            SEAM_ARTIFACT=out,
                        ),
                        timeout=20,
                    )
                    art = loads(open(out, encoding="ascii").read())
                    self.assertNotIn(
                        "TOPSECRET",
                        dumps(art),
                        f"{attempt} read the secret into the artifact",
                    )
                    self.assertNotEqual(
                        art["status"], "passed", f"{attempt} still reported passed"
                    )

    def test_policy_path_attributes_cannot_be_mutated(self):
        """`read_paths` and `write_paths` are read-only views.

        A frozenset rather than the live set, so `POLICY.read_paths.add(...)`
        raises instead of widening the policy. Asserted directly so the property
        is part of the contract rather than a side effect of the end-to-end test.
        """
        from seam.guard import POLICY

        self.assertIsInstance(POLICY.read_paths, frozenset)
        self.assertIsInstance(POLICY.write_paths, frozenset)
        with self.assertRaises(AttributeError):
            POLICY.read_paths.add("/tmp/anything")
        with self.assertRaises(AttributeError):
            POLICY.write_paths.add("/tmp/anything")

    def test_trusted_is_recognised_only_by_token_identity(self):
        """Assigning to `POLICY.trusted` must not stand the policy down.

        The audit hook decides whether to skip its checks by asking whether the
        token is one `trusted()` issued. The version before this kept the token
        on the attribute alone and tested `trusted is not None`, which any
        assignment satisfies -- a handler set `POLICY.trusted = object()` and
        every filesystem check was skipped while nothing was recorded in
        `fs_reads`.

        The assertion is on the hook's actual decision rather than on the
        membership set. Asserting the set directly would pass under both
        variants, because `trusted()` registers its token either way, so it
        would say nothing about whether the forgery works.
        """
        from seam.errors import Fault
        from seam.guard import POLICY, _LIVE_TOKENS, _audit, trusted

        saved = POLICY.trusted
        target = "/etc/hosts"
        try:
            POLICY.trusted = object()
            self.assertNotIn(POLICY.trusted, _LIVE_TOKENS)
            # The forged token must not let a read through.
            with self.assertRaises(Fault) as caught:
                _audit("open", (target, "r", -1))
            self.assertEqual(caught.exception.code, "file_read")

            # seam's own I/O still works, because it registers a real token.
            with trusted():
                self.assertIn(POLICY.trusted, _LIVE_TOKENS)
            # And the token is dropped on exit, so it cannot be reused later.
            self.assertNotIn(POLICY.trusted, _LIVE_TOKENS)
        finally:
            POLICY.trusted = saved

        # And once no block is active, a read is refused again.
        with self.assertRaises(Fault):
            _audit("open", (target, "r", -1))

    def test_allowlisted_read_is_recorded_but_not_digested(self):
        """A product declares a fixture path, reads it, and the read shows up as
        provenance. The digest must not move: fs_reads is about the host, not
        the run."""
        with tempfile.TemporaryDirectory() as directory:
            fixture = os.path.join(directory, "fixture.txt")
            with open(fixture, "w", encoding="ascii") as handle:
                handle.write("alpha\n")
            out = os.path.join(directory, "out.json")
            script = (
                "import os, sys\n"
                "from seam import Runtime, main\n"
                "from seam.guard import allow_read\n"
                # Registered before main, the way a product wires its runtime.
                "allow_read(os.environ['FIXTURE'])\n"
                "rt = Runtime(namespace='shop')\n"
                # The case scripts both ports, so the runtime must declare both.\n"
                "rt.port('payments', lambda: (lambda request: {'status': 'declined'}))\n"
                "rt.port('email', lambda: (lambda request: {'status': 'accepted'}))\n"
                "def message(ctx, body):\n"
                "    with open(os.environ['FIXTURE'], encoding='ascii') as handle:\n"
                "        ctx.emit('payments', {'line': handle.readline().strip()})\n"
                "rt.on('message', message)\n"
                "rt.on('remind', lambda ctx, body: None)\n"
                "code = main(rt)\n"
                "sys.stdout.write('CODE %s\\n' % code)\n"
            )
            case = json.loads(open(DECLINED, encoding="utf-8").read())
            case["arrivals"] = [{"at_ns": 0, "handler": "message", "body": {"sku": "book", "amount": 1}}]
            # The declined case matches the payment request on `amount`, so the
            # handler has to send a request the script can answer.
            case["ports"]["payments"]["replies"] = [
                {"match": {"line": "alpha"}, "response": {"status": "declined"}}
            ]
            case["assertions"] = [{"op": "port_response", "port": "payments", "i": 0, "match": {"status": "declined"}}]
            case_path = os.path.join(directory, "case.json")
            write_json(case_path, case)
            proc = run_product_case(
                case_path,
                "sim-checkout-declined",
                out,
                script,
                extra={"FIXTURE": fixture},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("CODE 0", proc.stdout)
            art = loads(open(out, encoding="ascii").read())
            self.assertEqual(art["status"], "passed")
            self.assertEqual(art["port_calls"][0]["request"], {"line": "alpha"})
            self.assertIn(os.path.realpath(fixture), art["fs_reads"])
            # fs_reads is provenance, so it must stay out of the digest body.
            self.assertNotIn("fs_reads", dumps(body_of(art)))
            self.assertEqual(digest(body_of(art)), art["digest"])

    def test_refuses_and_second_run_and_factory(self):
        with tempfile.TemporaryDirectory() as directory:
            out = os.path.join(directory, "out.json")
            proc = run_proc(
                [sys.executable, "-m", "seam.proof.checkout"],
                child_env(SEAM_SIM="1", SEAM_NAMESPACE="sim-checkout-declined", SEAM_ARTIFACT=out),
            )
            self.assertEqual(proc.returncode, 3, proc.stderr)
            self.assertEqual(loads(open(out, encoding="ascii").read())["fault"]["code"], "bad_env")

            proc = run_checkout(
                DECLINED,
                "sim-checkout-declined",
                out,
                extra={"SEAM_RECORD": "1"},
            )
            self.assertEqual(proc.returncode, 3, proc.stderr)
            self.assertEqual(loads(open(out, encoding="ascii").read())["fault"]["code"], "bad_env")

            case = json.loads(open(DECLINED, encoding="utf-8").read())
            case["ports"]["payments"] = {"mode": "generator"}
            gen = os.path.join(directory, "gen.json")
            write_json(gen, case)
            proc = run_checkout(gen, "sim-checkout-declined", out)
            self.assertEqual(proc.returncode, 3, proc.stderr)
            self.assertEqual(loads(open(out, encoding="ascii").read())["fault"]["code"], "unsupported")

            case = json.loads(open(DECLINED, encoding="utf-8").read())
            case["ports"]["payments"] = {"mode": "recording", "tape": "t.jsonl", "policy": "ordered"}
            cut = os.path.join(directory, "cut.json")
            write_json(cut, case)
            proc = run_checkout(cut, "sim-checkout-declined", out)
            self.assertEqual(proc.returncode, 3, proc.stderr)
            self.assertEqual(loads(open(out, encoding="ascii").read())["fault"]["code"], "cutoff_required")

    def test_second_run_and_factory_before_main(self):
        with tempfile.TemporaryDirectory() as directory:
            first = os.path.join(directory, "first.json")
            second = os.path.join(directory, "second.json")
            log = os.path.join(directory, "factories.log")
            script = (
                "import os, sys\n"
                "from seam import Refuse, main\n"
                "from seam.guard import trusted\n"
                "from seam.proof.checkout import build\n"
                "os.environ['SEAM_ARTIFACT'] = os.environ['ART1']\n"
                "c1 = main(build())\n"
                # Reading the artifact is the test's own I/O, not a handler's.\n"
                "with trusted():\n"
                "    before = open(os.environ['ART1'], 'rb').read()\n"
                "os.environ['SEAM_ARTIFACT'] = os.environ['ART2']\n"
                "c2 = main(build())\n"
                "with trusted():\n"
                "    after = open(os.environ['ART1'], 'rb').read()\n"
                "assert before == after\n"
                "sys.stdout.write('CODES %s %s\\n' % (c1, c2))\n"
            )
            env = child_env(
                SEAM_SIM="1",
                SEAM_NAMESPACE="sim-checkout-declined",
                SEAM_CASE=DECLINED,
                ART1=first,
                ART2=second,
            )
            proc = run_script(script, env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("CODES 0 3", proc.stdout)
            self.assertEqual(loads(open(second, encoding="ascii").read())["fault"]["code"], "second_run")
            self.assertEqual(loads(open(first, encoding="ascii").read())["digest"], RUN_DIGEST)

            factory = (
                "import os, sys\n"
                "from seam import Refuse, main\n"
                "from seam.proof.checkout import build\n"
                "rt = build()\n"
                "try:\n"
                "    rt.factories['payments']()\n"
                "except Refuse as err:\n"
                "    if err.code != 'live_factory_called':\n"
                "        raise SystemExit(4)\n"
                "else:\n"
                "    raise SystemExit(4)\n"
                "raise SystemExit(main(rt))\n"
            )
            out = os.path.join(directory, "factory.json")
            env = child_env(
                SEAM_SIM="1",
                SEAM_NAMESPACE="sim-checkout-declined",
                SEAM_CASE=DECLINED,
                SEAM_ARTIFACT=out,
                CHECKOUT_FACTORY_LOG=log,
            )
            proc = run_script(factory, env)
            self.assertEqual(proc.returncode, 3, proc.stderr)
            self.assertFalse(os.path.exists(log))
            self.assertEqual(loads(open(out, encoding="ascii").read())["fault"]["code"], "live_factory_called")

    def test_datetime_import_and_normal_file_under_guards(self):
        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, "note.txt")
            allowed = os.path.join(directory, "allowed.json")
            script = (
                "import os, sys\n"
                "from seam.guard import allow_read, allow_write, install_guards, trusted\n"
                "install_guards(write_paths=[os.environ['NOTE']], read_paths=[os.environ['NOTE']])\n"
                "install_guards()\n"
                "from datetime import datetime\n"
                "try:\n"
                "    datetime.now()\n"
                "    sys.stdout.write('datetime MISS\\n')\n"
                "except Exception as err:\n"
                "    sys.stdout.write('datetime %s %s\\n' % (err.code, err.op))\n"
                # A lazy import reads a module file. That must not fault.
                "import json\n"
                "sys.stdout.write('import json %s\\n' % json.dumps({'a': 1}))\n"
                "path = os.environ['NOTE']\n"
                "handle = open(path, 'w', encoding='ascii')\n"
                "handle.write('ok')\n"
                "handle.close()\n"
                "sys.stdout.write('file %s\\n' % open(path, encoding='ascii').read())\n"
                # An allowlisted path still faults when it is not allowlisted.\n"
                "try:\n"
                "    open(os.environ['DENIED'], 'w')\n"
                "    sys.stdout.write('denied MISS\\n')\n"
                "except Exception as err:\n"
                "    sys.stdout.write('denied %s %s\\n' % (err.code, err.op))\n"
                "try:\n"
                "    open(os.environ['DENIED'], encoding='ascii').read()\n"
                "    sys.stdout.write('deniedread MISS\\n')\n"
                "except Exception as err:\n"
                "    sys.stdout.write('deniedread %s %s\\n' % (err.code, err.op))\n"
                # Seam's own I/O is exempt.\n"
                "with trusted():\n"
                "    with open(os.environ['ALLOWED'], 'w', encoding='ascii') as h:\n"
                "        h.write('seam')\n"
                "    sys.stdout.write('trusted %s\\n' % open(os.environ['ALLOWED']).read())\n"
            )
            proc = run_script(
                script,
                child_env(NOTE=target, DENIED=allowed, ALLOWED=os.path.join(directory, "own.txt")),
                timeout=5,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("datetime real_clock datetime.now", proc.stdout)
            self.assertIn('import json {"a": 1}', proc.stdout)
            self.assertIn("file ok", proc.stdout)
            self.assertIn("denied file_write file.write", proc.stdout)
            self.assertIn("deniedread file_read file.read", proc.stdout)
            self.assertIn("trusted seam", proc.stdout)
            self.assertNotIn("MISS", proc.stdout)

    def test_live_module_does_not_simulate(self):
        proc = run_proc([sys.executable, "-m", "seam.proof.checkout"], child_env(), timeout=5)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")


if __name__ == "__main__":
    unittest.main()
