"""The same order handlers run with an explicit live adapter or isolated simulated inventory."""

import re

from seam import Fault, PortError, Runtime

from sim_starter.inventory import stock_world

SECOND = 1_000_000_000
ORDER_ID = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")


def order_body(body):
    if type(body) is not dict or set(body) != {"order"}:
        raise Fault("bad_order")
    key = body["order"]
    if type(key) is not str or not ORDER_ID.fullmatch(key):
        raise Fault("bad_order")
    return key


def validate_config(config):
    if type(config) is not dict or set(config) != {"fulfill_ns"}:
        raise ValueError("configuration requires fulfill_ns")
    if (
        type(config["fulfill_ns"]) is not int
        or not SECOND <= config["fulfill_ns"] <= 10 * SECOND
    ):
        raise ValueError("fulfill_ns must be between one and ten seconds")


def empty_state():
    return {"orders": {}}


def state_view(ctx):
    state = ctx.state
    if (
        type(state) is not dict
        or set(state) != {"orders"}
        or type(state["orders"]) is not dict
    ):
        raise Fault("bad_order_state")
    for key, row in state["orders"].items():
        order_body({"order": key})
        if type(row) is not dict or set(row) != {"status", "attempts", "ready_ns"}:
            raise Fault("bad_order_state")
        if type(row["status"]) is not str or row["status"] not in (
            "pending",
            "processing",
            "fulfilled",
            "rejected",
            "unknown",
        ):
            raise Fault("bad_order_state")
        if type(row["attempts"]) is not int or not 0 <= row["attempts"] <= 2:
            raise Fault("bad_order_state")
        if row["status"] == "pending" and row["attempts"] >= 2:
            raise Fault("bad_order_state")
        if row["status"] != "pending" and row["attempts"] == 0:
            raise Fault("bad_order_state")
        if row["status"] == "unknown" and row["attempts"] != 2:
            raise Fault("bad_order_state")
        if row["status"] in ("processing", "fulfilled"):
            if (
                row["attempts"] == 0
                or type(row["ready_ns"]) is not int
                or row["ready_ns"] < 0
            ):
                raise Fault("bad_order_state")
        elif row["ready_ns"] is not None:
            raise Fault("bad_order_state")
    return state


def reserve(ctx, body):
    key = order_body(body)
    state = state_view(ctx)
    if key not in state["orders"]:
        raise Fault("unknown_order")
    row = state["orders"][key]
    if row["status"] != "pending":
        return
    row["attempts"] += 1
    try:
        response = ctx.emit("stock", {"order": key})
        if response not in (
            {"order": key, "status": "reserved"},
            {"order": key, "status": "sold_out"},
        ):
            raise Fault("bad_stock_ack")
        if response["status"] == "sold_out":
            row["status"] = "rejected"
        else:
            row["status"] = "processing"
            row["ready_ns"] = ctx.now() + ctx.config["fulfill_ns"]
            ctx.schedule_after(ctx.config["fulfill_ns"], "fulfill", {"order": key})
    except PortError:
        row["status"] = "unknown" if row["attempts"] == 2 else "pending"
        if row["status"] == "pending":
            ctx.schedule_after(SECOND, "reserve", {"order": key})
    ctx.set_state(state)


def place(ctx, body):
    key = order_body(body)
    validate_config(ctx.config)
    state = state_view(ctx)
    if key in state["orders"]:
        return
    state["orders"][key] = {"status": "pending", "attempts": 0, "ready_ns": None}
    ctx.set_state(state)
    reserve(ctx, body)


def fulfill(ctx, body):
    key = order_body(body)
    state = state_view(ctx)
    if key not in state["orders"]:
        raise Fault("unknown_order")
    row = state["orders"][key]
    if row["status"] != "processing":
        return
    if ctx.now() < row["ready_ns"]:
        raise Fault("order_not_ready")
    row["status"] = "fulfilled"
    ctx.set_state(state)


def missing_inventory():
    raise RuntimeError("live orders require an explicit inventory adapter")


def build(*, inventory=None, fulfill_ns=2 * SECOND):
    config = {"fulfill_ns": fulfill_ns}
    validate_config(config)
    runtime = Runtime(
        namespace="starter-orders", config=config, initial_state=empty_state()
    )
    runtime.port("stock", missing_inventory if inventory is None else lambda: inventory)
    runtime.sim_world("inventory", stock_world, ports=("stock",))
    for name, handler in (("place", place), ("reserve", reserve), ("fulfill", fulfill)):
        runtime.on(name, handler)
    return runtime
