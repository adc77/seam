"""An isolated finite stock ledger commits reservations before simulating a lost acknowledgement."""

import re

from seam import Fault, PortError, World
from seam.canon import deep_copy

ORDER_ID = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")


def stock_world(ctx, data, config):
    state = {}

    def restore(value):
        if type(value) is not dict or set(value) != {
            "capacity",
            "reserved",
            "lost",
            "lose_ack",
        }:
            raise Fault("bad_inventory")
        if (
            type(value["capacity"]) is not int
            or not 0 <= value["capacity"] <= 2
            or type(value["lose_ack"]) is not bool
        ):
            raise Fault("bad_inventory")
        for field in ("reserved", "lost"):
            items = value[field]
            if type(items) is not list or any(
                type(key) is not str or not ORDER_ID.fullmatch(key) for key in items
            ):
                raise Fault("bad_inventory")
            if len(items) != len(set(items)) or len(items) > value["capacity"]:
                raise Fault("bad_inventory")
        if not set(value["lost"]) <= set(value["reserved"]):
            raise Fault("bad_inventory")
        if not value["lose_ack"] and value["lost"]:
            raise Fault("bad_inventory")
        state.clear()
        state.update(deep_copy(value))

    def call(body):
        if type(body) is not dict or set(body) != {"order"}:
            raise Fault("bad_order")
        key = body["order"]
        if type(key) is not str or not ORDER_ID.fullmatch(key):
            raise Fault("bad_order")
        if key not in state["reserved"]:
            if len(state["reserved"]) == state["capacity"]:
                return {"order": key, "status": "sold_out"}
            state["reserved"].append(key)
        if state["lose_ack"] and key not in state["lost"]:
            state["lost"].append(key)
            raise PortError("timeout")
        return {"order": key, "status": "reserved"}

    restore(data)
    return World({"stock": call}, lambda: deep_copy(state), restore)
