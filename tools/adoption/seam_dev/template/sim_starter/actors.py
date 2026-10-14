"""A buyer sees only their order and can select only product-owned place, wait, or finish choices."""

from seam import Fault
from seam.actors import ActorSystem, choice
from seam.canon import deep_copy
from seam.personas import persona

from sim_starter.product import SECOND, build as product_build


def population():
    return [
        {
            "id": name,
            "policy": "buyer_v1",
            "profile": {
                "persona": persona(
                    name,
                    ["Get my order fulfilled."],
                    constraints=["Only act on my own order."],
                )
            },
            "memory": {},
            "actions": ["place"],
            "start_ns": 0,
            "max_decisions": 12,
        }
        for name in ("buyer_a", "buyer_b")
    ]


def observe(actor, state, at_ns):
    return {"order": deep_copy(state["orders"].get(actor["id"]))}


def options(request):
    row = request["observation"]["order"]
    if row is not None and (
        type(row) is not dict
        or row.get("status")
        not in ("pending", "processing", "fulfilled", "rejected", "unknown")
    ):
        raise Fault("bad_actor_observation")
    if row is None:
        action = {"type": "place", "body": {"order": request["actor_id"]}}
        return [
            choice(
                "place",
                "Place my order now.",
                {"action": action, "memory": {}, "wake_after_ns": 0},
            )
        ]
    if row["status"] in ("fulfilled", "rejected", "unknown"):
        return [
            choice(
                "finish",
                "Finish decisions; retain my observed outcome, including failure or uncertainty.",
                {"action": None, "memory": {}, "wake_after_ns": None},
            )
        ]
    return [
        choice(
            "wait",
            "Wait one second for the product's reservation or fulfillment timer.",
            {"action": None, "memory": {}, "wake_after_ns": SECOND},
        )
    ]


def scripted_policy(request, rng):
    return {"choice": request["choices"][0]["id"]}


def translate(actor, action, decision_id):
    if action != {"type": "place", "body": {"order": actor["id"]}}:
        raise Fault("actor_action_forbidden")
    return {"handler": "place", "body": action["body"]}


def build():
    runtime = product_build()
    ActorSystem(
        {"buyer_v1": scripted_policy}, observe, translate, choices={"buyer_v1": options}
    ).install(runtime)
    return runtime
