"""A second product proof: inventory reservations against an isolated database copy."""

import sqlite3

from seam import Backend, Fault, Runtime


def inventory_backend(data, config):
    if type(data) is not dict or any(
        type(value) is not int or value < 0 for value in data.values()
    ):
        raise Fault("bad_value")
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE stock (sku TEXT PRIMARY KEY, quantity INTEGER NOT NULL)")
    db.executemany("INSERT INTO stock VALUES (?, ?)", sorted(data.items()))

    def handle(request):
        sku, quantity = request["sku"], request["quantity"]
        if type(sku) is not str or type(quantity) is not int or quantity < 1:
            raise Fault("bad_value")
        cursor = db.execute(
            "UPDATE stock SET quantity = quantity - ? WHERE sku = ? AND quantity >= ?",
            (quantity, sku, quantity),
        )
        return {"status": "reserved" if cursor.rowcount == 1 else "unavailable"}

    def snapshot():
        return dict(db.execute("SELECT sku, quantity FROM stock ORDER BY sku"))

    return Backend(handle, snapshot)


def _unconfigured_live_store():
    raise RuntimeError("The inventory proof requires a product-owned live store adapter")


def on_reserve(ctx, body):
    reply = ctx.emit("stock", {"sku": body["sku"], "quantity": body["quantity"]})
    orders = ctx.state
    orders[body["order"]] = reply["status"]
    ctx.set_state(orders)


def build():
    rt = Runtime(namespace="inventory")
    rt.port("stock", _unconfigured_live_store)
    rt.sim_port("stock", inventory_backend)
    rt.on("reserve", on_reserve)
    return rt
