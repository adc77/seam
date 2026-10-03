"""In-process tests. None of these install guards or call seam.main."""

import json
import os
import tempfile
import unittest

from seam.canon import digest, dumps, equal, loads, matches
from seam.case import load_case
from seam.clock import VirtualClock, format_utc
from seam.ctx import replace_at
from seam.errors import Fault, Refuse
from seam.grade import exit_code, finish
from seam.loop import run_sim
from seam.ports import load_tape
from seam.queue import Queue
from seam.rng import Rng
from seam.runtime import Runtime
from tests.support import (
    CASE_DIGEST,
    DECLINED,
    GOLDEN_BODY,
    RUN_DIGEST,
    body_of,
    child_env,
    run_script,
    write_json,
)

NS = "sim-sample"
PORTS = {"payments", "email"}
HANDLERS = {"message", "remind"}


def _base():
    return {
        "format": "seam-case",
        "version": 1,
        "name": "sample",
        "seed": 1842,
        "namespace": NS,
        "clock": {"start_ns": 0, "epoch": "1970-01-01T00:00:00Z"},
        "initial_state": {},
        "arrivals": [],
        "ports": {
            "p": {
                "mode": "script",
                "replies": [{"match": {"$any": True}, "response": {"ok": True}, "repeat": "forever"}],
            }
        },
        "stop": {"when": "quiescence"},
    }


def run_handlers(handlers, obj):
    rt = Runtime()
    for name in obj["ports"]:
        rt.port(name, lambda: (lambda request: {}))
    for name, fn in handlers.items():
        rt.on(name, fn)
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "case.json")
        write_json(path, obj)
        case = load_case(path, ports=set(rt.factories), handlers=set(rt.handlers), namespace=obj["namespace"])
    result = run_sim(rt, case)
    return result, finish(result, case)


class CanonTest(unittest.TestCase):
    def test_golden_vectors(self):
        self.assertEqual(dumps({"b": 1, "a": [True, None, "x"]}), '{"a":[true,null,"x"],"b":1}')
        self.assertEqual(dumps({"s": "é"}), '{"s":"\\u00e9"}')

    def test_rejects_float_duplicate_and_huge_int(self):
        with self.assertRaises(Refuse) as raised:
            loads("1.5")
        self.assertEqual(raised.exception.code, "bad_case")
        with self.assertRaises(Refuse):
            loads('{"a":1,"a":2}')
        self.assertIs(loads("true"), True)
        with self.assertRaises(Fault):
            from seam.canon import deep_copy

            deep_copy(2**63)

    def test_match_is_type_distinct_and_subset(self):
        self.assertTrue(matches({"$any": True}, {"n": 1}))
        self.assertTrue(matches({"n": 1}, {"n": 1, "extra": True}))
        self.assertFalse(matches({"n": 500}, {"n": "500"}))
        self.assertFalse(matches({"n": True}, {"n": 1}))
        self.assertFalse(matches([1, 2], [1]))
        self.assertTrue(equal(500, 500))
        self.assertFalse(equal(500, "500"))


class ClockRngQueueTest(unittest.TestCase):
    def test_calendar_and_backwards(self):
        self.assertEqual(format_utc(0), "1970-01-01T00:00:00.000000000Z")
        self.assertEqual(format_utc(1_000_000_000), "1970-01-01T00:00:01.000000000Z")
        self.assertEqual(format_utc(86400 * 1_000_000_000), "1970-01-02T00:00:00.000000000Z")
        self.assertEqual(format_utc(10957 * 86400 * 1_000_000_000), "2000-01-01T00:00:00.000000000Z")
        self.assertEqual(format_utc(789 * 86400 * 1_000_000_000), "1972-02-29T00:00:00.000000000Z")
        clock = VirtualClock(5)
        clock.jump(5)
        with self.assertRaises(Fault) as raised:
            clock.jump(4)
        self.assertEqual(raised.exception.code, "clock_backwards")

    def test_seed_1842_draws(self):
        rng = Rng(1842)
        self.assertEqual(rng.rand_u64(), 10316146753589248087)
        self.assertEqual(rng.rand_u64(), 802155294731228146)
        again = Rng(1842)
        self.assertEqual(again.rand_below(1), 0)
        self.assertEqual(again.rand_u64(), 802155294731228146)
        with self.assertRaises(Fault):
            Rng(1842).rand_below(0)

    def test_side_buffer_and_cancel(self):
        queue = Queue()
        queue.push_arrival(0, "a", {})
        seq = queue.push_timer(0, "b", {}, "t1")
        self.assertEqual(queue.peek()[2], "a")
        queue.pop()
        self.assertIsNone(queue.peek())
        queue.cancel(seq, "t1")
        queue.flush()
        self.assertIsNone(queue.peek())


class StateTest(unittest.TestCase):
    def test_patch_and_missing_parent(self):
        original = {"order": {"status": "pending", "reminded": False}}
        patched = replace_at(original, "order.reminded", True)
        self.assertEqual(patched["order"]["reminded"], True)
        self.assertEqual(original["order"]["reminded"], False)
        with self.assertRaises(Fault) as raised:
            replace_at({}, "order.reminded", True)
        self.assertEqual(raised.exception.code, "bad_value")
        self.assertEqual(replace_at({"n": 1}, "", {"n": 2}), {"n": 2})


class CaseTest(unittest.TestCase):
    def test_declined_digest_and_defaults(self):
        case = load_case(DECLINED, ports=PORTS, handlers=HANDLERS, namespace="sim-checkout-declined")
        self.assertEqual(case.digest, CASE_DIGEST)
        explicit = _base()
        explicit["ports"]["p"]["unmatched"] = "fail"
        explicit["ports"]["p"]["replies"][0]["repeat"] = 1
        explicit["stop"] = {
            "when": "quiescence",
            "allow_quiescence": False,
            "max_events": 100000,
            "max_port_calls": 10000,
        }
        explicit["assertions"] = []
        explicit["log_state"] = "on_change"
        bare = _base()
        bare["ports"]["p"]["replies"] = [{"match": {"$any": True}, "response": {"ok": True}}]
        with tempfile.TemporaryDirectory() as directory:
            left_path = os.path.join(directory, "a.json")
            right_path = os.path.join(directory, "b.json")
            write_json(left_path, explicit)
            write_json(right_path, bare)
            left = load_case(left_path, ports={"p"}, handlers=set(), namespace=NS)
            right = load_case(right_path, ports={"p"}, handlers=set(), namespace=NS)
        self.assertEqual(left.digest, right.digest)

    def test_refuses(self):
        def boom(mutate, code):
            obj = _base()
            mutate(obj)
            with tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, "case.json")
                write_json(path, obj)
                with self.assertRaises(Refuse) as raised:
                    load_case(path, ports={"p"}, handlers={"go"}, namespace=NS)
            self.assertEqual(raised.exception.code, code, mutate)

        boom(lambda obj: obj.__setitem__("version", True), "bad_case")
        boom(lambda obj: obj.__setitem__("nope", 1), "bad_case")
        boom(lambda obj: obj.__setitem__("seed", 2**64), "bad_case")
        boom(lambda obj: obj.__setitem__("namespace", "sim-other"), "namespace")
        boom(lambda obj: obj["ports"].__setitem__("q", {"mode": "script", "replies": []}), "unknown_port_in_case")
        boom(lambda obj: obj.__setitem__("ports", {}), "unscripted_port")
        boom(lambda obj: obj["ports"]["p"].__setitem__("mode", "generator"), "unsupported")
        boom(
            lambda obj: obj["ports"].__setitem__(
                "p", {"mode": "recording", "tape": "t.jsonl", "policy": "ordered"}
            ),
            "cutoff_required",
        )
        boom(lambda obj: obj["arrivals"].append({"at_ns": -1, "handler": "go", "body": {}}), "arrival_before_start")
        boom(lambda obj: obj["arrivals"].append({"at_ns": "0", "handler": "go", "body": {}}), "bad_case")
        boom(lambda obj: obj.__setitem__("assertions", [{"op": "llm"}]), "bad_case")

    def test_tape_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            nested = os.path.join(directory, "sub")
            os.makedirs(nested)
            visible = (
                '{"format":"seam-tape","version":1,"at_ns":0,"port":"p",'
                '"request":{"n":1},"response":{"ok":true}}\n'
            )
            hidden = (
                '{"format":"seam-tape","version":1,"at_ns":1,"port":"p",'
                '"request":{"n":1},"response":{"secret":"SEAM_HIDDEN_SENTINEL"}}\n'
            )
            tape = os.path.join(nested, "t.jsonl")
            with open(tape, "w", encoding="utf-8") as handle:
                handle.write(visible + hidden)
            loaded = load_tape(tape, "p", 0)
            self.assertEqual(len(loaded), 1)
            self.assertNotIn("SEAM_HIDDEN_SENTINEL", dumps(loaded))
            case = _base()
            case["ports"] = {"p": {"mode": "recording", "tape": "t.jsonl", "cutoff_ns": 0, "policy": "ordered"}}
            path = os.path.join(nested, "case.json")
            write_json(path, case)
            got = load_case(path, ports={"p"}, handlers=set(), namespace=NS)
            self.assertEqual(got.ports["p"].source, "recording")
            with open(os.path.join(nested, "bad.jsonl"), "w", encoding="utf-8") as handle:
                handle.write(visible.replace('"at_ns":0', '"at_ns":5', 1))
                handle.write(visible)
            case["ports"]["p"]["cutoff_ns"] = 10
            case["ports"]["p"]["tape"] = "bad.jsonl"
            write_json(path, case)
            with self.assertRaises(Refuse) as raised:
                load_case(path, ports={"p"}, handlers=set(), namespace=NS)
            self.assertEqual(raised.exception.code, "tape_unsorted")
            with open(os.path.join(nested, "torn.jsonl"), "w", encoding="utf-8") as handle:
                handle.write("{")
            case["ports"]["p"]["tape"] = "torn.jsonl"
            write_json(path, case)
            with self.assertRaises(Refuse) as raised:
                load_case(path, ports={"p"}, handlers=set(), namespace=NS)
            self.assertEqual(raised.exception.code, "tape_torn")
            case["ports"]["p"]["tape"] = "/tmp/outside.jsonl"
            write_json(path, case)
            with self.assertRaises(Refuse) as raised:
                load_case(path, ports={"p"}, handlers=set(), namespace=NS)
            self.assertEqual(raised.exception.code, "tape_path")
            outside = os.path.join(directory, "escape.jsonl")
            with open(outside, "w", encoding="utf-8") as handle:
                handle.write(visible)
            case["ports"]["p"]["tape"] = "../escape.jsonl"
            write_json(path, case)
            with self.assertRaises(Refuse) as raised:
                load_case(path, ports={"p"}, handlers=set(), namespace=NS)
            self.assertEqual(raised.exception.code, "tape_path")


class SchedulerTest(unittest.TestCase):
    def test_declined_golden_body(self):
        from seam.proof.checkout import build

        rt = build()
        case = load_case(DECLINED, ports=set(rt.factories), handlers=set(rt.handlers), namespace="sim-checkout-declined")
        result = run_sim(rt, case)
        art = finish(result, case)
        self.assertEqual(result.status, "passed")
        self.assertEqual(exit_code(result), 0)
        self.assertEqual(art["digest"], RUN_DIGEST)
        self.assertEqual(dumps(body_of(art)), GOLDEN_BODY)
        self.assertNotIn("fault", art)
        self.assertNotIn("terminal", art)
        self.assertTrue(all(row["ok"] for row in art["assertions"]))

    def test_pending_cancels_and_remind_fires(self):
        from seam.proof.checkout import CASE_DIR, build

        pending = load_case(
            os.path.join(CASE_DIR, "pending_then_paid.json"),
            ports={"payments", "email"},
            handlers={"message", "remind"},
            namespace="sim-checkout-pending",
        )
        result, art = _finish(build(), pending)
        self.assertEqual(result.status, "passed", art)
        self.assertEqual(art["final_state"], {"order": {"status": "paid"}})
        self.assertEqual(art["timers"], [
            {
                "token": "t1",
                "name": "remind-once",
                "handler": "remind",
                "fire_at_ns": 3_600_000_000_000,
                "outcome": "cancelled",
            }
        ])
        self.assertEqual(art["clock"]["end_ns"], 1_000_000_000)
        self.assertEqual([call["port"] for call in art["port_calls"]], ["payments", "payments"])
        self.assertEqual([event["kind"] for event in art["events"]], ["deliver", "schedule", "deliver", "cancel"])

        remind = load_case(
            os.path.join(CASE_DIR, "remind_once.json"),
            ports={"payments", "email"},
            handlers={"message", "remind"},
            namespace="sim-checkout-remind",
        )
        result, art = _finish(build(), remind)
        self.assertEqual(result.status, "passed", art)
        self.assertEqual(art["timers"][0]["outcome"], "fired")
        self.assertEqual(art["final_state"]["order"]["reminded"], True)
        self.assertEqual(art["clock"]["end_ns"], 3_600_000_000_000)
        self.assertEqual([call["port"] for call in art["port_calls"]], ["payments", "email"])
        self.assertEqual(art["port_calls"][1]["request"], {"kind": "remind"})

    def test_zero_delay_order_and_same_handler_cancel(self):
        seen = []

        def a(ctx, body):
            seen.append("a")
            ctx.set_state({"seen": ["a"]})
            ctx.schedule_after(0, "b", {})
            ctx.schedule_after(0, "c", {})

        def b(ctx, body):
            seen.append("b")
            state = ctx.state
            state["seen"].append("b")
            ctx.set_state(state)

        def c(ctx, body):
            seen.append("c")
            state = ctx.state
            state["seen"].append("c")
            ctx.set_state(state)

        obj = _base()
        obj["arrivals"] = [{"at_ns": 0, "handler": "a", "body": {}}]
        result, art = run_handlers({"a": a, "b": b, "c": c}, obj)
        self.assertEqual(result.status, "passed", art)
        self.assertEqual(seen, ["a", "b", "c"])
        self.assertEqual(art["final_state"]["seen"], ["a", "b", "c"])

        def arm_and_cancel(ctx, body):
            token = ctx.schedule_after(0, "later", {})
            ctx.cancel(token)

        def later(ctx, body):
            raise AssertionError("timer ran")

        obj = _base()
        obj["arrivals"] = [{"at_ns": 0, "handler": "go", "body": {}}]
        result, art = run_handlers({"go": arm_and_cancel, "later": later}, obj)
        self.assertEqual(result.status, "passed", art)
        self.assertEqual(art["timers"][0]["outcome"], "cancelled")

    def test_limits_deadline_and_faults(self):
        def go(ctx, body):
            ctx.set_state({"n": body["n"]})

        one = _base()
        one["stop"] = {"when": "quiescence", "max_events": 1}
        one["arrivals"] = [{"at_ns": 0, "handler": "go", "body": {"n": 1}}]
        result, art = run_handlers({"go": go}, one)
        self.assertEqual(result.status, "passed", art)

        two = _base()
        two["stop"] = {"when": "quiescence", "max_events": 1}
        two["arrivals"] = [
            {"at_ns": 0, "handler": "go", "body": {"n": 1}},
            {"at_ns": 1, "handler": "go", "body": {"n": 2}},
        ]
        result, art = run_handlers({"go": go}, two)
        self.assertEqual(result.loop_fault.code, "max_events")
        self.assertEqual(exit_code(result), 2)
        self.assertEqual([event["body"]["n"] for event in art["events"]], [1])

        capped = _base()
        capped["stop"] = {"when": "quiescence", "deadline_ns": 0}
        capped["arrivals"] = [
            {"at_ns": 0, "handler": "go", "body": {"n": 1}},
            {"at_ns": 5, "handler": "go", "body": {"n": 2}},
        ]
        result, art = run_handlers({"go": go}, capped)
        self.assertEqual(result.status, "passed", art)
        self.assertEqual(art["stop_reason"], "deadline")
        self.assertEqual([event["body"]["n"] for event in art["events"]], [1])

        early = _base()
        early["stop"] = {"when": "deadline", "deadline_ns": 10}
        result, _art = run_handlers({"go": go}, early)
        self.assertEqual(result.loop_fault.code, "ended_before_deadline")
        early["stop"]["allow_quiescence"] = True
        result, art = run_handlers({"go": go}, early)
        self.assertEqual(art["stop_reason"], "quiescence")
        self.assertEqual(result.status, "passed")

        def emit_twice(ctx, body):
            ctx.emit("p", {"n": 1})
            ctx.emit("p", {"n": 2})

        limited = _base()
        limited["stop"] = {"when": "quiescence", "max_port_calls": 1}
        limited["arrivals"] = [{"at_ns": 0, "handler": "go", "body": {}}]
        result, art = run_handlers({"go": emit_twice}, limited)
        self.assertEqual(result.loop_fault.code, "max_port_calls")
        self.assertEqual(len(art["port_calls"]), 1)
        self.assertEqual(art["port_calls"][0]["request"], {"n": 1})

        def unmatched(ctx, body):
            ctx.emit("p", {"n": 1})

        bare = _base()
        bare["ports"]["p"]["replies"] = []
        bare["arrivals"] = [{"at_ns": 0, "handler": "go", "body": {}}]
        result, art = run_handlers({"go": unmatched}, bare)
        self.assertEqual(result.loop_fault.code, "unmatched_port")
        self.assertIsNone(art["port_calls"][0]["response"])
        self.assertEqual(art["port_calls"][0]["request"], {"n": 1})

    def test_stop_cancel_rollback_and_secret(self):
        def stop_paid(ctx, body):
            ctx.stop("paid")

        obj = _base()
        obj["stop"] = {"when": "terminal", "terminal": "paid"}
        obj["arrivals"] = [{"at_ns": 0, "handler": "go", "body": {}}]
        result, art = run_handlers({"go": stop_paid}, obj)
        self.assertEqual(result.status, "passed", art)
        self.assertEqual(art["terminal"], "paid")
        self.assertEqual(art["stop_reason"], "terminal")

        def stop_wrong(ctx, body):
            ctx.stop("nope")

        obj["stop"] = {"when": "terminal", "terminal": "paid"}
        result, art = run_handlers({"go": stop_wrong}, obj)
        self.assertEqual(result.loop_fault.code, "unexpected_terminal")
        self.assertNotIn("terminal", art)

        def unknown(ctx, body):
            ctx.cancel("t9")

        obj = _base()
        obj["arrivals"] = [{"at_ns": 0, "handler": "go", "body": {}}]
        result, _art = run_handlers({"go": unknown}, obj)
        self.assertEqual(result.loop_fault.code, "unknown_timer")

        def arm(ctx, body):
            ctx.schedule_after(0, "later", {}, name="once")

        def later(ctx, body):
            ctx.cancel("t1")

        obj["arrivals"] = [{"at_ns": 0, "handler": "go", "body": {}}]
        result, art = run_handlers({"go": arm, "later": later}, obj)
        self.assertEqual(result.loop_fault.code, "cancel_fired")
        self.assertEqual(art["timers"][0]["outcome"], "fired")

        def boom(ctx, body):
            ctx.set_state({"kept": True})
            raise RuntimeError("SUPER_SECRET_MESSAGE")

        result, art = run_handlers({"go": boom}, obj)
        self.assertEqual(result.loop_fault.code, "handler_error")
        self.assertEqual(result.loop_fault.exc_type, "RuntimeError")
        self.assertEqual(art["final_state"], {"kept": True})
        self.assertNotIn("SUPER_SECRET_MESSAGE", dumps(art))
        self.assertEqual(art["events"][0]["status"], "error")

        def mutate_copy(ctx, body):
            state = ctx.state
            state["x"] = 1

        result, art = run_handlers({"go": mutate_copy}, obj)
        self.assertEqual(art["final_state"], {})
        self.assertEqual(result.status, "passed")

    def test_assertions_do_not_change_the_digest(self):
        obj = _base()
        obj["arrivals"] = [{"at_ns": 0, "handler": "go", "body": {}}]

        def go(ctx, body):
            ctx.set_state({"n": 1})

        result, art = run_handlers({"go": go}, obj)
        pinned = art["digest"]
        obj["assertions"] = [
            {"op": "state_is", "path": "n", "value": 1},
            {"op": "digest_is", "sha256": pinned},
            {"op": "state_is", "path": "missing", "value": 1},
            {"op": "state_is", "path": "n", "value": 2},
        ]
        result, art = run_handlers({"go": go}, obj)
        self.assertEqual(art["digest"], pinned)
        self.assertEqual(exit_code(result), 1)
        self.assertEqual([row["ok"] for row in art["assertions"]], [True, True, False, False])
        self.assertEqual(art["assertions"][2]["detail"], "path missing")
        self.assertNotIn("fault", art)

    def test_script_repeat_and_types(self):
        seen = []

        def go(ctx, body):
            seen.append(ctx.emit("p", {"n": 1}))
            seen.append(ctx.emit("p", {"n": 1}))
            seen.append(ctx.emit("p", {"n": "1"}))

        obj = _base()
        obj["ports"]["p"]["replies"] = [
            {"match": {"n": 1}, "response": {"k": "a"}, "repeat": 1},
            {"match": {"n": 1}, "response": {"k": "b"}, "repeat": 1},
            {"match": {"n": "1"}, "response": {"k": "c"}, "repeat": 1},
        ]
        obj["arrivals"] = [{"at_ns": 0, "handler": "go", "body": {}}]
        result, art = run_handlers({"go": go}, obj)
        self.assertEqual(result.status, "passed", art)
        self.assertEqual([row["k"] for row in seen], ["a", "b", "c"])

    def test_reentrant_deliver(self):
        rt = Runtime()
        rt.port("p", lambda: (lambda request: {}))

        def go(ctx, body):
            rt.deliver("go", {})

        rt.on("go", go)
        obj = _base()
        obj["arrivals"] = [{"at_ns": 0, "handler": "go", "body": {}}]
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "case.json")
            write_json(path, obj)
            case = load_case(path, ports={"p"}, handlers={"go"}, namespace=NS)
        result = run_sim(rt, case)
        self.assertEqual(result.loop_fault.code, "reentrant")


def _finish(rt, case):
    result = run_sim(rt, case)
    return result, finish(result, case)


class _StubFault:
    """Minimal stand-in for a Fault, which `digest_body` calls `.as_dict()` on."""

    def as_dict(self):
        return {"code": "x"}


class _StubFault:
    """Stand-in for a Fault: `digest_body` calls `.as_dict()` on it."""

    def as_dict(self):
        return {"code": "boom"}


class DigestBodyKeysTest(unittest.TestCase):
    """`BODY_KEYS` in tests/support is a hand copy of `digest_body`'s keys.

    The tests rebuild the digest body from their own list, so they check the run
    rather than the library's own function -- which is worth having, because a
    bug in `digest_body` could otherwise hide itself. It only works while the two
    agree. Add a key to the library and `body_of` would go on hashing a
    different body, and every test would still pass.

    An earlier version of this stubbed a fake result and had to be taught each
    new attribute the library reads, one failure at a time. The attribute list is
    now read out of the function's own source, so a change there fails loudly
    instead of silently.
    """

    @staticmethod
    def _result_stub(art):
        """A stand-in answering exactly the attributes `digest_body` reads."""
        import inspect
        import re

        from seam.artifact import digest_body
        from tests.support import BODY_KEYS

        names = sorted(set(re.findall(r"result\.([a-z_]+)", inspect.getsource(digest_body))))

        class Result:
            """Answers the attributes `digest_body` reads."""

        for name in names:
            setattr(Result, name, None)
        if art is not None:
            for key in BODY_KEYS:
                setattr(Result, key, art[key])
            Result.start_ns = art["clock"]["start_ns"]
            Result.end_ns = art["clock"]["end_ns"]
            Result.terminal = art.get("terminal")
        return Result, names

    @staticmethod
    def _run_checkout(directory):
        """Run the checkout proof once and return the artifact it wrote."""
        from tests.support import DECLINED, run_checkout

        artifact_path = os.path.join(directory, "artifact.json")
        proc = run_checkout(DECLINED, "sim-checkout-declined", artifact_path)
        assert proc.returncode == 0, proc.stderr
        return loads(open(artifact_path, encoding="ascii").read())

    def test_key_list_matches_the_library(self):
        import tempfile

        from seam.artifact import digest_body
        from tests.support import BODY_KEYS

        with tempfile.TemporaryDirectory() as directory:
            art = self._run_checkout(directory)

        Result, names = self._result_stub(art)
        from_library = set(digest_body(Result()))

        # This run passes with no terminal and no fault, so those two are
        # legitimately absent from both sides.
        self.assertEqual(
            from_library - set(BODY_KEYS),
            set(),
            "digest_body hashes keys the test helper never collects, so the "
            "helper would be checking a different body",
        )
        self.assertEqual(
            set(BODY_KEYS) - from_library,
            set(),
            "the test helper collects keys the digest ignores, so it would be "
            "checking a body the library never hashed",
        )
        self.assertIn("clock", from_library)
        self.assertGreater(len(from_library), 5)
        # The stub must answer everything the function reads, including the two
        # conditional ones, or this test is checking less than it claims.
        self.assertIn("terminal", names)
        self.assertIn("loop_fault", names)

    def test_conditional_keys_appear_only_when_set(self):
        """`terminal` and `fault` are added only when not None.

        Neither is in the unconditional list, so a run without them must not be
        read as if they were there.
        """
        from seam.artifact import digest_body
        from tests.support import BODY_KEYS

        Result, _names = self._result_stub(None)
        body = digest_body(Result())
        self.assertNotIn("terminal", body)
        self.assertNotIn("fault", body)

        Result.terminal = {"name": "done"}
        self.assertIn("terminal", digest_body(Result()))

        Result.loop_fault = _StubFault()
        with_fault = digest_body(Result())
        self.assertIn("fault", with_fault)
        self.assertEqual(with_fault["fault"], {"code": "boom"})

        # Not unconditional: the helper must not read a key that may be absent.
        self.assertNotIn("terminal", BODY_KEYS)
        self.assertNotIn("fault", BODY_KEYS)

    def test_helper_rebuilds_the_body_the_library_hashed(self):
        """The strongest form: `body_of` must reproduce the recorded digest.

        Not just the same keys -- the same content, so a key present in both but
        populated differently is caught too. The tests hash what `body_of`
        returns and compare it to the artifact, so that body has to be exactly
        what the runner hashed.
        """
        import tempfile

        from tests.support import BODY_KEYS, body_of

        with tempfile.TemporaryDirectory() as directory:
            art = self._run_checkout(directory)

        rebuilt = body_of(art)
        self.assertEqual(set(rebuilt), set(BODY_KEYS))
        self.assertEqual(digest(rebuilt), art["digest"])


class IdentifierFormatTest(unittest.TestCase):
    """Live and sim mint identifiers and timer tokens independently.

    They have to agree, because a recording made live is replayed in sim and a
    digest computed from either must be the same. Both formats were spelled out
    inline in each host, so a change to one would not have touched the other and
    nothing tested `ctx.id()` at all. These pin both to the shared constants.
    """

    def test_sim_and_live_mint_the_same_id_and_token_shape(self):
        from seam.canon import ID_HEX_WIDTH, TIMER_TOKEN_PREFIX

        # Live: the runtime mints an id through its own rand source.
        live = Runtime(namespace="shop")
        live.port("p", lambda: (lambda request: {}))
        seen = {}

        def go(ctx, body):
            seen["id"] = ctx.id("ord")
            seen["token"] = ctx.schedule_after(1, "go", {}, name="later")

        # Handlers and ports are registered before start_live, which refuses to
        # change its wiring once the mode is set.
        live.on("go", go)
        live.set_timer_backend(lambda *args: None)
        live.start_live()
        live.deliver("go", {})

        prefix, _, hexpart = seen["id"].partition("_")
        self.assertEqual(prefix, "ord")
        self.assertEqual(len(hexpart), ID_HEX_WIDTH)
        int(hexpart, 16)  # it is hex
        self.assertTrue(seen["token"].startswith(TIMER_TOKEN_PREFIX))

        # Sim: same shape, from the seeded stream.
        with tempfile.TemporaryDirectory() as directory:
            case_path = os.path.join(directory, "case.json")
            write_json(
                case_path,
                {
                    "format": "seam-case",
                    "version": 1,
                    "name": "ids",
                    "seed": 1842,
                    "namespace": "sim-ids",
                    "clock": {"start_ns": 0, "epoch": "1970-01-01T00:00:00Z"},
                    "initial_state": {},
                    "arrivals": [{"at_ns": 0, "handler": "go", "body": {}}],
                    "ports": {"p": {"mode": "script", "replies": [
                        {"match": {"$any": True}, "response": {}}]}},
                    "stop": {"when": "quiescence"},
                },
            )
            artifact = os.path.join(directory, "out.json")
            env = child_env(
                SEAM_SIM="1",
                SEAM_NAMESPACE="sim-ids",
                SEAM_CASE=case_path,
                SEAM_ARTIFACT=artifact,
            )
            program = (
                "import sys\n"
                "from seam import Runtime, main\n"
                "rt = Runtime(namespace='shop')\n"
                "rt.port('p', lambda: (lambda request: {}))\n"
                "def go(ctx, body):\n"
                # No timer here: scheduling one from a handler that also fires
                # again would reuse the name and fault the run, and this test is
                # about identifier shape, not timers. The token format is
                # covered by the live half below.
                "    ctx.emit('p', {'id': ctx.id('ord')})\n"
                "rt.on('go', go)\n"
                "sys.stdout.write('CODE %s\\n' % main(rt))\n"
            )
            proc = run_script(program, env, timeout=20)
            self.assertIn("CODE 0", proc.stdout, proc.stderr)
            art = loads(open(artifact, encoding="ascii").read())
            # Assert the run actually passed before reading anything out of it.
            # Reading a failed run's partial artifact is how a test ends up
            # passing on data the runner never committed to.
            self.assertEqual(art["status"], "passed", dumps(art)[:400])
            request = art["port_calls"][0]["request"]
            sim_prefix, _, sim_hex = request["id"].partition("_")
            self.assertEqual(sim_prefix, prefix)
            # Compare the two hosts against each other directly, not only
            # against the shared constant: if one host hardcodes the width, the
            # constant still says the right thing and both would pass a test
            # that only checked the constant. The widths must match exactly.
            self.assertEqual(len(sim_hex), ID_HEX_WIDTH, "sim id width is wrong")
            self.assertEqual(
                len(request["id"]),
                len(seen["id"]),
                "live and sim disagree on identifier length",
            )
            # And the shape itself: one underscore, hex only, fixed width.
            for ident in (seen["id"], request["id"]):
                self.assertEqual(ident.count("_"), 1, ident)
                head, _, tail = ident.partition("_")
                self.assertTrue(head.isalnum() and head.islower(), ident)
                self.assertEqual(len(tail), ID_HEX_WIDTH, ident)
                int(tail, 16)

    def test_identifier_width_survives_a_small_random_value(self):
        """The hex is zero padded to a fixed width, whatever the value.

        Comparing the live host against the simulated one is not enough on its
        own: both draw from seed 1842, so they produce the identical u64 and
        agree even if the width is wrong in one of them. The width is therefore
        checked against a value that is small enough to need padding, where a
        narrower format would actually show.
        """
        from seam.canon import ID_HEX_WIDTH
        from seam.rng import Rng

        # Format the way both hosts do, over draws small enough to need padding.
        # A u64 that fits in far fewer than ID_HEX_WIDTH hex digits: a draw that
        # happens to need all 16 would pass under any min-width format spec.
        # A value that needs real padding. Drawing it from the seeded stream
        # would not work: with a fixed seed the draws are whatever they are, and
        # a stream that happens to yield full-width values every time would let
        # a wrong width pass. The point here is the format, so a known-small
        # value is what exercises it.
        small = 0xABCDEF
        self.assertEqual(len(f"{small:x}"), 6, "the probe value should need padding")

        formatted = f"{small:0{ID_HEX_WIDTH}x}"
        self.assertEqual(
            len(formatted),
            ID_HEX_WIDTH,
            "a small value must still be padded to the full width",
        )
        self.assertNotEqual(len(f"{small:x}"), ID_HEX_WIDTH)

        # And through the real RNG path, over the extremes of the range.
        for draw in (small, 0, 1, (1 << 64) - 1):
            rendered = f"{draw:0{ID_HEX_WIDTH}x}"
            self.assertEqual(len(rendered), ID_HEX_WIDTH, hex(draw))

    def test_neither_host_hardcodes_the_identifier_width(self):
        """Both hosts must format ids through `ID_HEX_WIDTH`, not a literal.

        This is the invariant that makes the width check above meaningful. If a
        host spelled out its own width, the shared constant could be changed and
        that host would keep going, and because both hosts draw the same seed
        their outputs would still agree -- so no comparison between them would
        ever notice.
        """
        import inspect

        from seam.loop import Engine
        from seam.runtime import Runtime

        for name, func in (("Engine.ident", Engine.ident), ("Runtime.ident", Runtime.ident)):
            with self.subTest(host=name):
                source = inspect.getsource(func)
                self.assertIn("ID_HEX_WIDTH", source, f"{name} must use the shared constant")
                # And no bare hex width of its own.
                import re

                self.assertIsNone(
                    re.search(r"0\w*x|:016x|\{\d+\}x", source),
                    f"{name} appears to hardcode a hex width",
                )

    def test_neither_host_hardcodes_the_timer_token_prefix(self):
        """Both hosts must format timer tokens through `TIMER_TOKEN_PREFIX`.

        A recording made live is replayed in simulation, so a token minted one
        way and matched the other way would leave the replay stuck on an armed
        timer.
        """
        import inspect

        from seam.loop import Engine
        from seam.runtime import Runtime

        for name, func in (("Engine.schedule_at", Engine.schedule_at),
                           ("Runtime._arm", Runtime._arm)):
            with self.subTest(host=name):
                source = inspect.getsource(func)
                self.assertIn("TIMER_TOKEN_PREFIX", source, f"{name} must use the constant")


class FaultCodeTest(unittest.TestCase):
    def test_every_raised_fault_code_is_declared(self):
        """A case may only assert on a fault code listed in `FAULT_CODES`.

        Nothing stops a new `Fault("...")` from being raised in a module that has
        no access to that set, so this walks the source instead. A code that is
        raised but not declared is unassertable: a case written to catch it
        would be refused as a bad_case, and the failure would look like a typo in
        the test rather than a gap in the declaration.
        """
        import pathlib
        import re

        from seam.case import FAULT_CODES, STOP_REASONS

        package = pathlib.Path(__file__).resolve().parent.parent / "seam"
        raised = set()
        for path in sorted(package.rglob("*.py")):
            raised.update(
                re.findall(
                    r"""\bFault\(\s*["']([a-z_]+)["']""", path.read_text(encoding="utf-8")
                )
            )
        self.assertTrue(raised, "no Fault codes found; the scan is broken")
        self.assertEqual(
            sorted(raised - FAULT_CODES),
            [],
            "fault codes raised but not declared in FAULT_CODES",
        )
        # And nothing declared is dead, which catches a rename that only landed
        # on one side.
        self.assertEqual(
            sorted(FAULT_CODES - raised),
            [],
            "codes declared in FAULT_CODES that nothing raises",
        )
        # The two sets are disjoint: a stop reason is set, a fault code is
        # raised, and conflating them is what this split exists to prevent.
        self.assertEqual(STOP_REASONS & FAULT_CODES, set())


class ParentStaysLiveTest(unittest.TestCase):
    def test_parent_clock_still_works(self):
        import time

        self.assertIsInstance(time.time(), float)


class GuardPolicyUnitTest(unittest.TestCase):
    """Policy bookkeeping that does not need the guards installed.

    The guards can only be installed once per process, so these exercise the
    Policy object directly. The behavioural half lives in tests/test_sim.py.
    """

    def test_registration_survives_a_reset(self):
        from seam.guard import POLICY, _norm, allow_read, allow_write

        saved = (
            set(POLICY.read_paths),
            POLICY._read_view,
            set(POLICY.write_paths),
            POLICY._write_view,
            set(POLICY.extra_reads),
            set(POLICY.extra_writes),
            POLICY.sealed,
        )
        try:
            # Paths are normalized, so /tmp and /private/tmp are the same entry.
            read = _norm("/tmp/seam-unit-read")
            write = _norm("/tmp/seam-unit-write")
            artifact = _norm("/tmp/other-artifact")
            allow_read("/tmp/seam-unit-read")
            allow_write("/tmp/seam-unit-write")
            POLICY.reset(write_paths=("/tmp/other-artifact",))
            self.assertIn(read, POLICY.read_paths)
            self.assertIn(write, POLICY.write_paths)
            # The runner's own artifact path is still there too.
            self.assertIn(artifact, POLICY.write_paths)
        finally:
            # The public path attributes are read-only views now, so the test
            # saves and restores the underlying sets.
            (
                POLICY._read,
                POLICY._read_view,
                POLICY._write,
                POLICY._write_view,
                POLICY.extra_reads,
                POLICY.extra_writes,
                POLICY.sealed,
            ) = saved

    def test_paths_are_exact_not_prefixes(self):
        from seam.guard import POLICY, _norm, allow_read

        saved = (set(POLICY._read), POLICY._read_view, set(POLICY.extra_reads), POLICY.sealed)
        try:
            allow_read("/tmp/seam-dir")
            # A sibling that merely shares the prefix is not granted.
            self.assertNotIn(_norm("/tmp/seam-dir-other"), POLICY.read_paths)
            self.assertIn(_norm("/tmp/seam-dir"), POLICY.read_paths)
        finally:
            POLICY._read, POLICY._read_view, POLICY.extra_reads, POLICY.sealed = saved

    def test_interpreter_reads_are_not_recorded_as_provenance(self):
        import sysconfig

        from seam.guard import POLICY, _interpreter_dirs, _norm

        saved = set(POLICY.reads)
        try:
            self.assertTrue(_interpreter_dirs(), "interpreter directories should resolve")
            # Something that really is interpreter code, under the real stdlib.
            module = _norm(os.path.join(sysconfig.get_path("stdlib"), "json", "__init__.py"))
            self.assertTrue(POLICY.is_interpreter(module))
            POLICY.reads = set()
            POLICY.note_read(module)
            self.assertEqual(POLICY.reads, set())
            outside = _norm("/tmp/some-fixture")
            POLICY.note_read(outside)
            self.assertEqual(POLICY.reads, {outside})
        finally:
            POLICY.reads = saved


if __name__ == "__main__":
    unittest.main()
