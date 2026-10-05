"""Two product ports sharing one isolated database and resumable in-flight work."""

import sqlite3

from seam import Fault, Runtime, World


def store_world(ctx, data, config):
    if type(data) is not dict or set(data) != {"balance"} or type(data["balance"]) is not int:
        raise Fault("bad_value")
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE ledger (balance INTEGER NOT NULL)")
    db.execute("INSERT INTO ledger VALUES (?)", (data["balance"],))
    metadata = {"boot_id": ctx.id("db"), "boot_ns": ctx.now()}

    def write(request):
        if type(request) is not dict or type(request.get("delta")) is not int:
            raise Fault("bad_value")
        db.execute("UPDATE ledger SET balance = balance + ?", (request["delta"],))
        return {"status": "written"}

    def read(request):
        return {"balance": db.execute("SELECT balance FROM ledger").fetchone()[0], "at_ns": ctx.now()}

    def snapshot():
        return {"balance": db.execute("SELECT balance FROM ledger").fetchone()[0], **metadata}

    def restore(state):
        if (type(state) is not dict or set(state) != {"balance", "boot_id", "boot_ns"}
                or type(state["balance"]) is not int or state["boot_id"] != metadata["boot_id"]
                or state["boot_ns"] != metadata["boot_ns"]):
            raise Fault("bad_value")
        db.execute("UPDATE ledger SET balance = ?", (state["balance"],))

    return World({"write": write, "read": read}, snapshot, restore, db.close)


def _live_store():
    raise RuntimeError("The shared-store proof has no configured live database")


def on_write(ctx, body):
    job = ctx.id("job")
    ctx.emit("write", {"delta": body["delta"]})
    later = ctx.schedule_after(10, "read", {}, name="read-once")
    due = ctx.schedule_after(100, "expired", {}, name="due-once")
    ghost = ctx.schedule_after(5, "expired", {}, name="cancelled-once")
    ctx.cancel(ghost)
    ctx.set_state({"job": job, "due": due, "later": later,
                   "after_write": ctx.emit("read", {}), "draw": ctx.rand_below(1000)})


def on_observe(ctx, body):
    ctx.cancel(ctx.state["due"])
    ctx.patch("observed", ctx.emit("read", {}))
    ctx.patch("new_token", ctx.schedule_after(0, "done", {}, name="done-once"))


def on_read(ctx, body):
    ctx.patch("final", ctx.emit("read", {}))
    ctx.patch("next_job", ctx.id("job"))


def on_done(ctx, body):
    ctx.patch("done", True)


def on_expired(ctx, body):
    raise Fault("bad_value")


def build():
    rt = Runtime(namespace="ledger")
    for port in ("write", "read"):
        rt.port(port, _live_store)
    rt.sim_world("ledger", store_world, ports=("write", "read"))
    for name, handler in (("write", on_write), ("observe", on_observe), ("read", on_read),
                          ("done", on_done), ("expired", on_expired)):
        rt.on(name, handler)
    return rt
