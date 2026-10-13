"""Independent choice-policy proof with a code-owned not-before gate."""

from seam import Runtime
from seam.actors import ActorSystem, choice


def options(request):
    memory = request["memory"]
    if request["observation"]["count"] >= request["profile"]["goal"]:
        return [
            choice(
                "finish",
                "Accept the observed completed goal.",
                {
                    "action": None,
                    "memory": memory,
                    "wake_after_ns": None,
                },
            )
        ]
    ready = request["profile"]["ready_ns"]
    if request["at_ns"] < ready:
        return [
            choice(
                "wait",
                "Wait for the code-owned eligibility deadline.",
                {
                    "action": None,
                    "memory": memory,
                    "wake_after_ns": ready - request["at_ns"],
                },
            )
        ]
    return [
        choice(
            "add",
            "Add one item toward the goal.",
            {
                "action": {"type": "add", "body": {"amount": 1}},
                "memory": {"actions": memory["actions"] + 1},
                "wake_after_ns": 0,
            },
        ),
        choice(
            "wait",
            "Defer this eligible action.",
            {
                "action": None,
                "memory": memory,
                "wake_after_ns": 2,
            },
        ),
    ]


def policy(request, rng):
    return {"choice": request["choices"][0]["id"]}


def build():
    runtime = Runtime()

    def add(ctx, body):
        ctx.patch("count", ctx.state["count"] + body["amount"])

    runtime.on("add", add)
    ActorSystem(
        {"choice_v1": policy},
        lambda actor, state, at: {"count": state["count"]},
        lambda actor, action, token: {"handler": "add", "body": action["body"]},
        choices={"choice_v1": options},
    ).install(runtime)
    return runtime
