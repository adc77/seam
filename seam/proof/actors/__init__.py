"""A domain-neutral feedback loop using the optional actor world."""

from seam import Fault, Runtime
from seam.actors import ActorSystem


def counter_policy(request, rng):
    memory = request["memory"]
    if request["observation"]["count"] >= request["profile"]["goal"]:
        return {"action": None, "memory": memory, "wake_after_ns": None}
    memory["actions"] += 1
    return {
        "action": {"type": "add", "body": {"amount": rng.rand_below(3) + 1}},
        "memory": memory,
        "wake_after_ns": request["profile"]["delay_ns"],
    }


def observe(actor, state, at_ns):
    return {"count": state["count"]}


def translate(actor, action, decision_id):
    if action["type"] != "add":
        raise Fault("actor_action_forbidden")
    return {"handler": "add", "body": action["body"]}


def build():
    rt = Runtime()

    def add(ctx, body):
        ctx.patch("count", ctx.state["count"] + body["amount"])

    rt.on("add", add)
    ActorSystem({"counter_v1": counter_policy}, observe, translate).install(rt)
    return rt
