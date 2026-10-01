"""Subprocess tests. The parent must not install guards or call seam.main."""

import json
import os
import stat
import tempfile
import unittest

from seam.canon import digest, dumps, loads
from seam.proof.checkout import CASE_DIR
from tests.support import (
    CASE_DIGEST,
    DECLINED,
    GOLDEN_BODY,
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
            self.assertEqual(stat.S_IMODE(os.stat(first).st_mode), 0o644)
            art = loads(data.decode("ascii"))
            self.assertEqual(art["digest"], RUN_DIGEST)
            self.assertEqual(art["case_digest"], CASE_DIGEST)
            self.assertEqual(art["package_version"], "0.1.0")
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
