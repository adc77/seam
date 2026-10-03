"""Live mode, in process. These tests must not install guards or call seam.main."""

import os
import tempfile
import unittest

from seam import NoTimerBackend, Runtime
from seam.canon import loads
from seam.errors import Fault, Refuse
from seam.proof.checkout import build


class LiveTest(unittest.TestCase):
    def setUp(self):
        import time

        self.assertIsInstance(time.time(), float)
        self._saved = {}
        for key in list(os.environ):
            if key.startswith("SEAM_") or key.startswith("CHECKOUT_"):
                self._saved[key] = os.environ.pop(key)

    def tearDown(self):
        for key in list(os.environ):
            if key.startswith("SEAM_") or key.startswith("CHECKOUT_"):
                os.environ.pop(key, None)
        os.environ.update(self._saved)

    def test_sim_flag_has_one_rule_and_every_reader_agrees(self):
        """`in_sim()` and `start_live()` must answer alike about SEAM_SIM.

        The flag used to be read three ways in one file -- `== "1"`,
        `not in (None, "0")`, `!= "1"` -- so `SEAM_SIM=true` made `in_sim()`
        report False while `start_live` refused: one environment, two answers.
        A later version over-corrected and refused on *any* value including
        "0", which disagreed in the opposite direction.

        The rule is now: only "1" is a simulation; "0" and "" and unset all mean
        live; anything else non-empty is refused rather than quietly running
        live, since a misspelt intent to simulate would otherwise get no error.
        """
        from seam.runtime import in_sim

        saved = os.environ.pop("SEAM_SIM", None)
        try:
            expectations = [
                # (value, in_sim, start_live accepted?)
                (None, False, True),
                ("", False, True),
                ("0", False, True),
                ("1", True, False),
                ("true", False, False),
                ("yes", False, False),
                ("2", False, False),
            ]
            for value, expect_sim, expect_live in expectations:
                label = "unset" if value is None else repr(value)
                with self.subTest(SEAM_SIM=label):
                    if value is None:
                        os.environ.pop("SEAM_SIM", None)
                    else:
                        os.environ["SEAM_SIM"] = value
                    self.assertEqual(in_sim(), expect_sim, f"in_sim() for {label}")
                    rt = Runtime(namespace="shop")
                    if expect_live:
                        rt.start_live()
                        self.assertEqual(rt.mode, "live")
                    else:
                        with self.assertRaises(Refuse) as raised:
                            rt.start_live()
                        self.assertEqual(raised.exception.code, "bad_env")
        finally:
            if saved is None:
                os.environ.pop("SEAM_SIM", None)
            else:
                os.environ["SEAM_SIM"] = saved

    def test_refuses_a_sim_environment_and_a_sim_namespace(self):
        rt = Runtime()
        os.environ["SEAM_CASE"] = "case.json"
        with self.assertRaises(Refuse) as raised:
            rt.start_live()
        self.assertEqual(raised.exception.code, "bad_env")
        del os.environ["SEAM_CASE"]
        os.environ["SEAM_SIM"] = "1"
        with self.assertRaises(Refuse) as raised:
            Runtime().start_live()
        self.assertEqual(raised.exception.code, "bad_env")
        del os.environ["SEAM_SIM"]
        os.environ["SEAM_RECORD"] = "1"
        with self.assertRaises(Refuse) as raised:
            Runtime().start_live()
        self.assertEqual(raised.exception.code, "bad_env")
        os.environ["SEAM_RECORD"] = "2"
        with self.assertRaises(Refuse) as raised:
            Runtime().start_live()
        self.assertEqual(raised.exception.code, "bad_env")
        with self.assertRaises(Refuse) as raised:
            Runtime(namespace="sim-shop")
        self.assertEqual(raised.exception.code, "namespace")

    def test_factory_is_called_once_on_first_emit(self):
        with tempfile.TemporaryDirectory() as directory:
            log = os.path.join(directory, "factories.log")
            os.environ["CHECKOUT_FACTORY_LOG"] = log
            rt = build()
            self.assertEqual(rt.factories["payments"].calls, 0)
            rt.start_live()
            self.assertEqual(rt.factories["payments"].calls, 0)
            rt.deliver("message", {"sku": "book", "amount": 500})
            rt.deliver("message", {"sku": "book", "amount": 500})
            self.assertEqual(rt.factories["payments"].calls, 1)
            self.assertEqual(rt.factories["email"].calls, 0)
            self.assertEqual(rt.state_copy(), {"order": {"status": "declined"}})
            self.assertEqual(open(log, encoding="ascii").read(), "payments\n")

    def test_deliver_before_start_and_reentry(self):
        rt = Runtime()
        rt.on("go", lambda ctx, body: None)
        with self.assertRaises(Refuse) as raised:
            rt.deliver("go", {})
        self.assertEqual(raised.exception.code, "bad_env")
        holder = {}

        def go(ctx, body):
            holder["rt"].deliver("go", {})

        rt = Runtime()
        holder["rt"] = rt
        rt.port("p", lambda: (lambda request: {}))
        rt.on("go", go)
        rt.start_live()
        with self.assertRaises(Fault) as raised:
            rt.deliver("go", {})
        self.assertEqual(raised.exception.code, "reentrant")

    def test_recording_fsync_redact_and_off(self):
        with tempfile.TemporaryDirectory() as directory:
            tape = os.path.join(directory, "live.jsonl")
            absent = os.path.join(directory, "absent.jsonl")
            os.environ["SEAM_RECORD"] = "0"
            rt = build()
            rt.start_live()
            rt.deliver("message", {"sku": "book", "amount": 500})
            self.assertFalse(os.path.exists(absent))
            self.assertFalse(os.path.exists(os.path.join(os.getcwd(), "seam-artifact.json")))

            calls = []
            real = os.fsync

            def spy(fd):
                calls.append(fd)
                return real(fd)

            os.fsync = spy
            try:
                os.environ["SEAM_RECORD"] = "1"
                os.environ["SEAM_ARTIFACT"] = tape
                rt = build()
                rt.redact(lambda value: {"status": "redacted"} if type(value) is dict and "status" in value else value)
                rt.start_live()
                rt.deliver("message", {"sku": "book", "amount": 500})
            finally:
                os.fsync = real
            self.assertTrue(calls)
            line = loads(open(tape, encoding="ascii").read().splitlines()[0])
            self.assertEqual(line["format"], "seam-tape")
            self.assertEqual(line["version"], 1)
            self.assertEqual(line["port"], "payments")
            self.assertEqual(line["request"], {"amount": 500})
            self.assertEqual(line["response"], {"status": "redacted"})
            self.assertIsInstance(line["at_ns"], int)
            self.assertEqual(rt.state_copy()["order"]["status"], "declined")
            rt._record.close()

    def test_timer_backend_and_stamp(self):
        seen = []
        rt = Runtime(namespace="shop")
        rt.port("p", lambda: (lambda request: {"ok": True}))
        rt.set_timer_backend(lambda *args: seen.append(args))

        def go(ctx, body):
            token = ctx.schedule_after(5, "go", {"a": 1}, name="later")
            ctx.cancel(token)
            stamped = ctx.stamp({"id": 1})
            seen.append(stamped)
            seen.append(ctx.namespace)

        rt.on("go", go)
        rt.start_live()
        rt.deliver("go", {})
        self.assertEqual(seen[-1], "shop")
        self.assertEqual(seen[-2], {"id": 1, "_seam_ns": "shop"})
        self.assertEqual(seen[0][0][:1], "t")
        self.assertEqual(seen[0][2], "go")

        bare = Runtime()
        bare.port("p", lambda: (lambda request: {}))

        def need(ctx, body):
            ctx.schedule_after(1, "need", {})

        bare.on("need", need)
        bare.start_live()
        with self.assertRaises(NoTimerBackend):
            bare.deliver("need", {})


if __name__ == "__main__":
    unittest.main()
