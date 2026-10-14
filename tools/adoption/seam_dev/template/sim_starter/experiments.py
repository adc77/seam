"""Prepare paired synthetic exports with public capture APIs and measure actual product outcomes."""

from seam import capture_bundle, write_capture_case
from seam.actors import population as actor_population

from sim_starter.actors import population
from sim_starter.product import SECOND, empty_state, validate_config

MODULE = "sim_starter.simulation"
ACTOR_WORLDS = ("actors",)
METRICS = {
    "fulfilled": "orders",
    "rejected": "orders",
    "pending": "orders",
    "ack_unknown": "orders",
    "allocated": "items",
    "unfinished_actors": "actors",
}


def prepare(directory, parameters, seed):
    if type(parameters) is not dict or set(parameters) != {
        "capacity",
        "lose_ack",
        "fulfill_ns",
        "until_ns",
    }:
        raise ValueError("parameters require capacity, lose_ack, fulfill_ns, until_ns")
    if (
        type(parameters["capacity"]) is not int
        or not 0 <= parameters["capacity"] <= 2
        or type(parameters["lose_ack"]) is not bool
    ):
        raise ValueError("capacity must be 0..2 and lose_ack a boolean")
    if (
        type(parameters["until_ns"]) is not int
        or not 1 <= parameters["until_ns"] <= 10 * SECOND
    ):
        raise ValueError("until_ns must be positive and at most ten seconds")
    config = {"fulfill_ns": parameters["fulfill_ns"]}
    validate_config(config)
    document = capture_bundle(
        MODULE,
        as_of_ns=0,
        until_ns=parameters["until_ns"],
        config=config,
        initial_state=empty_state(),
        datasets={
            "actors": {
                "as_of_ns": 0,
                "data": actor_population(population(), seed=seed),
            },
            "inventory": {
                "as_of_ns": 0,
                "data": {
                    "capacity": parameters["capacity"],
                    "reserved": [],
                    "lost": [],
                    "lose_ack": parameters["lose_ack"],
                },
            },
        },
        arrivals=[{"at_ns": 0, "handler": "actors_start", "body": {}}],
        assertions=[{"op": "event_count", "handler": "place", "times": 2}],
        product_data={"synthetic": True},
    )
    return write_capture_case(
        document,
        directory,
        module=MODULE,
        name="starter-buyers",
        namespace="sim-starter-orders",
        ports={
            "stock": {"mode": "world", "world": "inventory"},
            "actors": {"mode": "world", "world": "actors"},
        },
        worlds={"inventory": {"dataset": "inventory"}, "actors": {"dataset": "actors"}},
        finalize=({"handler": "actors_finish", "body": {}},),
        same_time_order="timers_first",
        seed=seed,
    )


def measure(artifact):
    if (
        artifact["status"] != "passed"
        or not artifact["world_states"]["actors"]["sealed"]
    ):
        raise ValueError("metrics require a passed run and sealed observation window")
    rows = list(artifact["final_state"]["orders"].values())
    return {
        "fulfilled": sum(row["status"] == "fulfilled" for row in rows),
        "rejected": sum(row["status"] == "rejected" for row in rows),
        "pending": sum(row["status"] in ("pending", "processing") for row in rows),
        "ack_unknown": sum(row["status"] == "unknown" for row in rows),
        "allocated": len(artifact["world_states"]["inventory"]["reserved"]),
        "unfinished_actors": sum(
            row["next_ns"] is not None
            for row in artifact["world_states"]["actors"]["actors"].values()
        ),
    }


def variants():
    return [
        {
            "name": "healthy",
            "parameters": {
                "capacity": 2,
                "lose_ack": False,
                "fulfill_ns": 2 * SECOND,
                "until_ns": 8 * SECOND,
            },
        },
        {
            "name": "scarce",
            "parameters": {
                "capacity": 1,
                "lose_ack": True,
                "fulfill_ns": 2 * SECOND,
                "until_ns": 8 * SECOND,
            },
        },
    ]
