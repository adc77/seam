"""Fake checkout. Two ports, two handlers. This is the proof, not a sample to delete."""

import os

from seam import Runtime
from seam.errors import Fault

CASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases")
HOUR_NS = 3_600_000_000_000


def _mark(port):
    path = os.environ.get("CHECKOUT_FACTORY_LOG")
    if not path:
        return
    with open(path, "a", encoding="ascii") as handle:
        handle.write(port + "\n")


def make_payments():
    _mark("payments")
    return lambda request: {"status": "declined"}


def make_email():
    _mark("email")
    return lambda request: {"status": "accepted"}


def _leak(kind):
    if kind == "socket":
        import socket

        socket.create_connection(("203.0.113.1", 80), timeout=1)
    elif kind == "clock":
        import time

        time.time()
    elif kind == "uuid":
        import uuid

        uuid.uuid4()
    elif kind == "thread":
        import threading

        threading.Thread(target=lambda: None).start()
    elif kind == "urandom":
        import os as os_mod

        os_mod.urandom(1)
    elif kind == "devrandom":
        open("/dev/urandom", "rb").read(1)
    elif kind == "subprocess":
        import subprocess

        subprocess.run(["/bin/echo", "should-not-run"], check=False)
    elif kind == "datetime":
        import datetime

        datetime.datetime.now()
    elif kind == "random":
        import random

        random.random()
    elif kind == "random_instance":
        # A fresh Random never calls the module functions, so the instance
        # methods are the ones that have to be blocked.
        import random

        random.Random(7).getrandbits(64)
    elif kind == "ctypes":
        # libc from here means a real clock and real syscalls.
        import ctypes

        ctypes.CDLL(None).getpid()
    elif kind == "perf_counter":
        import time

        time.perf_counter()
    elif kind == "process_time":
        import time

        time.process_time()
    elif kind == "file_read":
        open(os.environ["SEAM_LEAK_FILE"], encoding="ascii").read(1)
    elif kind == "file_write":
        open(os.environ["SEAM_LEAK_FILE"], "w", encoding="ascii").write("x")
    elif kind == "os_open_read":
        fd = os.open(os.environ["SEAM_LEAK_FILE"], os.O_RDONLY)
        os.close(fd)
    elif kind == "os_stat":
        os.stat(os.environ["SEAM_LEAK_FILE"])
    elif kind == "listdir":
        os.listdir(os.environ["SEAM_LEAK_DIR"])
    elif kind == "scandir":
        [entry.name for entry in os.scandir(os.environ["SEAM_LEAK_DIR"])]
    else:
        raise Fault("bad_value")


def on_message(ctx, body):
    if type(body) is dict and "leak" in body:
        _leak(body["leak"])
    decision = ctx.emit("payments", {"amount": body["amount"]})
    status = decision["status"]
    if status == "declined":
        ctx.set_state({"order": {"status": "declined"}})
        return
    if status == "pending":
        token = ctx.schedule_after(
            HOUR_NS,
            "remind",
            {"amount": body["amount"]},
            name="remind-once",
        )
        ctx.set_state({"order": {"status": "pending", "reminded": False, "token": token}})
        return
    if status == "paid":
        current = ctx.state
        order = current.get("order") if type(current) is dict else None
        token = order.get("token") if type(order) is dict else None
        if type(token) is str:
            ctx.cancel(token)
        ctx.set_state({"order": {"status": "paid"}})
        return
    raise Fault("bad_value")


def on_remind(ctx, body):
    current = ctx.state
    order = current.get("order") if type(current) is dict else None
    if type(order) is not dict or order.get("status") != "pending":
        return
    ctx.emit("email", {"kind": "remind"})
    ctx.patch("order.reminded", True)


def build():
    rt = Runtime()
    rt.port("payments", make_payments)
    rt.port("email", make_email)
    rt.on("message", on_message)
    rt.on("remind", on_remind)
    return rt
