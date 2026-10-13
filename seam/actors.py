"""Optional, offline reactive actors built on shared worlds and ordinary scheduled handlers."""

import hashlib
import re

from seam import Fault, World
from seam.canon import INT64_MAX, MAX_STRING, deep_copy, digest, dumps, equal
from seam.ctx import assert_int
from seam.rng import Rng
from seam.runtime import require_sync, require_sync_result

ACTOR_ID = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
MAX_ACTORS = 100
MAX_DECISIONS = 1000
MAX_DRAWS = 1024


def _fields(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        raise Fault("bad_actor")


def _identifier(value):
    if type(value) is not str or not ACTOR_ID.fullmatch(value):
        raise Fault("bad_actor")
    return value


def _bounded(value):
    value = deep_copy(value)
    if len(dumps(value).encode("ascii")) > MAX_STRING:
        raise Fault("actor_size")
    return value


def population(actors, *, seed=0, tape=None):
    """Create a strict actor dataset; an explicit tape selects recorded replay, including an empty tape."""
    document = {
        "format": "seam-actors",
        "version": 1,
        "seed": seed,
        "actors": actors,
        "mode": "rules" if tape is None else "replay",
        "tape": [] if tape is None else tape,
    }
    return _population(document)


def inference_population(actors, *, seed=0, responses=()):
    """Create an external-decision dataset; unprovided decisions stop at an explicit request barrier."""
    return _population(
        {
            "format": "seam-actors",
            "version": 1,
            "seed": seed,
            "actors": actors,
            "mode": "inference",
            "tape": list(responses),
        }
    )


def _population(value):
    _fields(value, ("format", "version", "seed", "actors", "mode", "tape"))
    if (
        value["format"] != "seam-actors"
        or type(value["version"]) is not int
        or value["version"] != 1
    ):
        raise Fault("bad_actor")
    assert_int(value["seed"], minimum=0)
    if type(value["mode"]) is not str or value["mode"] not in (
        "rules",
        "replay",
        "inference",
    ):
        raise Fault("bad_actor")
    actors, tape = value["actors"], value["tape"]
    if type(actors) is not list or not 1 <= len(actors) <= MAX_ACTORS:
        raise Fault("bad_actor")
    if (
        type(tape) is not list
        or len(tape) > MAX_DECISIONS
        or (value["mode"] == "rules" and tape)
    ):
        raise Fault("bad_actor")
    ids = set()
    for actor in actors:
        _fields(
            actor,
            (
                "id",
                "policy",
                "profile",
                "memory",
                "actions",
                "start_ns",
                "max_decisions",
            ),
        )
        actor_id = _identifier(actor["id"])
        _identifier(actor["policy"])
        if actor_id in ids or type(actor["profile"]) is not dict:
            raise Fault("bad_actor")
        ids.add(actor_id)
        assert_int(actor["start_ns"], minimum=0)
        assert_int(actor["max_decisions"], minimum=1, maximum=MAX_DECISIONS)
        actions = actor["actions"]
        if type(actions) is not list or len(actions) > 32:
            raise Fault("bad_actor")
        for action in actions:
            _identifier(action)
        if len(set(actions)) != len(actions):
            raise Fault("bad_actor")
    for record in tape:
        if value["mode"] == "inference":
            _fields(record, ("request", "decision"))
            _request(record["request"])
            _validate_selection(record["request"], _decision(record["decision"]))
        else:
            _record(record)
    return _bounded(value)


def _decision(value):
    if type(value) is dict and set(value) == {"choice"}:
        _identifier(value["choice"])
        return _bounded(value)
    _fields(value, ("action", "memory", "wake_after_ns"))
    action, delay = value["action"], value["wake_after_ns"]
    if action is not None:
        _fields(action, ("type", "body"))
        _identifier(action["type"])
    if delay is not None:
        assert_int(delay, minimum=0)
    return _bounded(value)


def choice(choice_id, description, decision):
    """Offer one product-owned action, memory transition, and next-decision delay."""
    _identifier(choice_id)
    if (
        type(description) is not str
        or not description.strip()
        or len(description) > 512
        or any(ord(char) < 32 for char in description)
    ):
        raise Fault("bad_actor_choice")
    decision = _decision(decision)
    if "choice" in decision:
        raise Fault("bad_actor_choice")
    if decision["action"] is not None and decision["wake_after_ns"] is None:
        raise Fault("actor_choice_followup")
    if decision["action"] is None and decision["wake_after_ns"] == 0:
        raise Fault("actor_choice_progress")
    return _bounded({"id": choice_id, "description": description, "decision": decision})


def _validate_selection(request, decision):
    if "choices" not in request:
        if "choice" in decision:
            raise Fault("actor_choice_forbidden")
        return
    if set(decision) != {"choice"} or decision["choice"] not in {
        option["id"] for option in request["choices"]
    }:
        raise Fault("actor_choice_forbidden")


def _record(value):
    fields = {"request", "decision", "rng_after", "delivery"}
    if type(value) is not dict or set(value) not in (fields, fields | {"resolution"}):
        raise Fault("bad_actor")
    request = value["request"]
    _request(request)
    decision = _decision(value["decision"])
    _validate_selection(request, decision)
    if ("choices" in request) != ("resolution" in value):
        raise Fault("bad_actor")
    if "resolution" in value:
        resolved = _decision(value["resolution"])
        if "choice" in resolved:
            raise Fault("bad_actor")
        offered = next(
            item for item in request["choices"] if item["id"] == decision["choice"]
        )
        if (
            not equal(resolved["action"], offered["action"])
            or resolved["wake_after_ns"] != offered["wake_after_ns"]
        ):
            raise Fault("actor_choice_changed")
    assert_int(
        value["rng_after"],
        minimum=request["rng_before"],
        maximum=request["rng_before"] + MAX_DRAWS,
    )
    delivery = value["delivery"]
    if delivery is not None:
        _fields(delivery, ("handler", "body"))
        _identifier(delivery["handler"])


def _request(request):
    fields = {
        "actor_id",
        "policy",
        "step",
        "at_ns",
        "profile",
        "memory",
        "observation",
        "rng_before",
        "seed",
        "actions",
    }
    if type(request) is not dict or set(request) not in (fields, fields | {"choices"}):
        raise Fault("bad_actor")
    _identifier(request["actor_id"])
    _identifier(request["policy"])
    for field in ("step", "at_ns", "rng_before", "seed"):
        assert_int(request[field], minimum=0)
    if type(request["profile"]) is not dict:
        raise Fault("bad_actor")
    if type(request["actions"]) is not list or len(request["actions"]) > 32:
        raise Fault("bad_actor")
    for action in request["actions"]:
        _identifier(action)
    if len(set(request["actions"])) != len(request["actions"]):
        raise Fault("bad_actor")
    if "choices" in request:
        options = request["choices"]
        if type(options) is not list or not 1 <= len(options) <= 32:
            raise Fault("bad_actor_choice")
        ids = set()
        for option in options:
            _fields(option, ("id", "description", "action", "wake_after_ns"))
            item = choice(
                option["id"],
                option["description"],
                {
                    "action": option["action"],
                    "memory": None,
                    "wake_after_ns": option["wake_after_ns"],
                },
            )
            if item["id"] in ids:
                raise Fault("bad_actor_choice")
            ids.add(item["id"])
            action = option["action"]
            delay = option["wake_after_ns"]
            if action is not None and action["type"] not in request["actions"]:
                raise Fault("actor_action_forbidden")
            if delay is not None and request["at_ns"] + delay > INT64_MAX:
                raise Fault("bad_actor_choice")


class ActorRandom:
    """A bounded per-decision stream independent of the product's RNG."""

    def __init__(self, seed, actor_id, counter):
        assert_int(seed, minimum=0)
        _identifier(actor_id)
        assert_int(counter, minimum=0)
        raw = hashlib.sha256(
            f"seam.actors.v1:{seed}:{actor_id}".encode("ascii")
        ).digest()
        self._rng = Rng(int.from_bytes(raw[:8], "little"))
        self._rng.counter = counter
        self._start = counter

    @property
    def counter(self):
        return self._rng.counter

    def rand_u64(self):
        if self.counter - self._start >= MAX_DRAWS:
            raise Fault("actor_rng_budget")
        return self._rng.rand_u64()

    def rand_below(self, n):
        assert_int(n, minimum=1)
        limit = (2**64 // n) * n
        while True:
            draw = self.rand_u64()
            if draw < limit:
                return draw % n


class ActorSystem:
    """Register an actor world, projected observations, authorized action mapping, and scheduler driver."""

    def __init__(self, policies, observe, translate, *, name="actors", choices=None):
        _identifier(name)
        if len(name) > 25 or type(policies) is not dict or not policies:
            raise Fault("bad_actor")
        for key, fn in policies.items():
            _identifier(key)
            require_sync(fn)
        require_sync(observe)
        require_sync(translate)
        self.policies = dict(policies)
        self.observe = observe
        self.translate = translate
        self.name = name
        if choices is None:
            choices = {}
        if type(choices) is not dict or not set(choices) <= set(policies):
            raise Fault("bad_actor_choice")
        for fn in choices.values():
            require_sync(fn)
        self.choices = dict(choices)

    def _options(self, request):
        base = {key: value for key, value in request.items() if key != "choices"}
        options = self.choices[request["policy"]](deep_copy(base))
        require_sync_result(options)
        if type(options) is not list or not 1 <= len(options) <= 32:
            raise Fault("bad_actor_choice")
        result, ids = [], set()
        for option in options:
            _fields(option, ("id", "description", "decision"))
            option = choice(option["id"], option["description"], option["decision"])
            action, delay = (
                option["decision"]["action"],
                option["decision"]["wake_after_ns"],
            )
            if option["id"] in ids:
                raise Fault("bad_actor_choice")
            ids.add(option["id"])
            if action is not None and action["type"] not in request["actions"]:
                raise Fault("actor_action_forbidden")
            if delay is not None and request["at_ns"] + delay > INT64_MAX:
                raise Fault("bad_actor_choice")
            result.append(option)
        return result

    @staticmethod
    def _menu(options):
        return [
            {
                "id": item["id"],
                "description": item["description"],
                "action": item["decision"]["action"],
                "wake_after_ns": item["decision"]["wake_after_ns"],
            }
            for item in options
        ]

    def _resolve(self, request, decision):
        _validate_selection(request, decision)
        if request["policy"] not in self.choices:
            if "choices" in request:
                raise Fault("actor_choice_forbidden")
            return decision
        options = self._options(request)
        if "choices" not in request or not equal(
            request["choices"], self._menu(options)
        ):
            raise Fault("actor_choice_changed")
        return next(
            item["decision"] for item in options if item["id"] == decision["choice"]
        )

    def install(self, runtime):
        names = tuple(f"{self.name}_{suffix}" for suffix in ("start", "step", "finish"))
        if (
            runtime.mode is not None
            or self.name in runtime.factories
            or self.name in runtime.worlds
        ):
            raise Fault("bad_actor")
        if any(name in runtime.handlers for name in names):
            raise Fault("bad_actor")
        runtime.port(self.name, self._live_disabled)
        runtime.sim_world(self.name, self._world, ports=(self.name,))
        runtime.on(names[0], self._start)
        runtime.on(names[1], self._step)
        runtime.on(names[2], self._finish)

    @staticmethod
    def _live_disabled():
        raise Fault("actor_live_forbidden")

    def _world(self, ctx, data, config):
        spec = _population(data)
        definitions = {actor["id"]: actor for actor in spec["actors"]}
        if any(actor["policy"] not in self.policies for actor in definitions.values()):
            raise Fault("bad_actor")
        state = {
            "format": "seam-actor-state",
            "version": 1,
            "population_sha256": digest(spec),
            "started": False,
            "sealed": False,
            "actors": {},
            "decisions": [],
        }
        for actor_id, actor in definitions.items():
            state["actors"][actor_id] = {
                "memory": deep_copy(actor["memory"]),
                "step": 0,
                "rng_counter": 0,
                "next_ns": actor["start_ns"],
            }

        def actor_context(actor_id, step):
            _identifier(actor_id)
            assert_int(step, minimum=0)
            if not state["started"] or state["sealed"] or actor_id not in definitions:
                raise Fault("actor_state")
            current = state["actors"][actor_id]
            if step != current["step"] or current["next_ns"] != ctx.now():
                raise Fault("actor_stale")
            return definitions[actor_id], current

        def call(request):
            if type(request) is not dict or type(request.get("op")) is not str:
                raise Fault("bad_actor")
            op = request["op"]
            if op == "start":
                _fields(request, ("op",))
                if state["started"] or state["sealed"]:
                    raise Fault("actor_state")
                if any(actor["start_ns"] < ctx.now() for actor in definitions.values()):
                    raise Fault("bad_actor")
                state["started"] = True
                return [
                    {"actor_id": actor["id"], "step": 0, "at_ns": actor["start_ns"]}
                    for actor in spec["actors"]
                ]
            if op == "seal":
                _fields(request, ("op",))
                if not state["started"] or state["sealed"]:
                    raise Fault("actor_state")
                if any(
                    row["next_ns"] is not None and row["next_ns"] <= ctx.now()
                    for row in state["actors"].values()
                ):
                    raise Fault("actor_pending")
                if spec["mode"] in ("replay", "inference") and len(
                    state["decisions"]
                ) != len(spec["tape"]):
                    raise Fault("actor_tape_unused")
                state["sealed"] = True
                return {"sealed": True}
            if op == "context":
                _fields(request, ("op", "actor_id", "step"))
                actor, current = actor_context(request["actor_id"], request["step"])
                return {
                    "id": actor["id"],
                    "profile": actor["profile"],
                    "memory": current["memory"],
                }
            if op != "decide":
                raise Fault("bad_actor")
            _fields(request, ("op", "actor_id", "step", "observation"))
            actor, current = actor_context(request["actor_id"], request["step"])
            if (
                current["step"] >= actor["max_decisions"]
                or len(state["decisions"]) >= MAX_DECISIONS
            ):
                raise Fault("actor_budget")
            policy_input = {
                "actor_id": actor["id"],
                "policy": actor["policy"],
                "step": current["step"],
                "at_ns": ctx.now(),
                "profile": actor["profile"],
                "memory": current["memory"],
                "observation": request["observation"],
                "rng_before": current["rng_counter"],
                "seed": spec["seed"],
                "actions": actor["actions"],
            }
            if actor["policy"] in self.choices:
                policy_input["choices"] = self._menu(self._options(policy_input))
            rng = ActorRandom(spec["seed"], actor["id"], current["rng_counter"])
            if spec["mode"] == "inference":
                index = len(state["decisions"])
                if index >= len(spec["tape"]):
                    raise Fault(
                        "actor_inference_required", op=dumps(_bounded(policy_input))
                    )
                recorded = spec["tape"][index]
                if not equal(policy_input, recorded["request"]):
                    raise Fault("actor_tape_mismatch")
                decision = _decision(recorded["decision"])
                counter = rng.counter
            elif spec["mode"] == "replay":
                index = len(state["decisions"])
                if index >= len(spec["tape"]):
                    raise Fault("actor_tape_exhausted")
                recorded = spec["tape"][index]
                if not equal(policy_input, recorded["request"]):
                    raise Fault("actor_tape_mismatch")
                decision = _decision(recorded["decision"])
                counter = recorded["rng_after"]
            else:
                decision = self.policies[actor["policy"]](deep_copy(policy_input), rng)
                require_sync_result(decision)
                decision = _decision(decision)
                counter = rng.counter
            resolved = self._resolve(policy_input, decision)
            action, delay = resolved["action"], resolved["wake_after_ns"]
            if action is not None and action["type"] not in actor["actions"]:
                raise Fault("actor_action_forbidden")
            next_ns = None if delay is None else ctx.now() + delay
            if next_ns is not None and next_ns > INT64_MAX:
                raise Fault("bad_actor")
            delivery = None
            if action is not None:
                descriptor = {
                    "id": actor["id"],
                    "profile": actor["profile"],
                    "memory": resolved["memory"],
                }
                decision_id = "act_" + digest(policy_input)[:32]
                delivery = self.translate(
                    deep_copy(descriptor), deep_copy(action), decision_id
                )
                require_sync_result(delivery)
                _fields(delivery, ("handler", "body"))
                _identifier(delivery["handler"])
                if delivery["handler"] in {
                    f"{self.name}_{suffix}" for suffix in ("start", "step", "finish")
                }:
                    raise Fault("actor_action_forbidden")
                delivery = _bounded(delivery)
            record = {
                "request": policy_input,
                "decision": decision,
                "rng_after": counter,
                "delivery": delivery,
            }
            if "choices" in policy_input:
                record["resolution"] = resolved
            _record(record)
            if spec["mode"] == "replay" and not equal(record, recorded):
                raise Fault("actor_tape_mismatch")
            updated = deep_copy(state)
            updated["actors"][actor["id"]] = {
                "memory": resolved["memory"],
                "step": current["step"] + 1,
                "rng_counter": counter,
                "next_ns": next_ns,
            }
            updated["decisions"].append(record)
            updated = _bounded(updated)
            state.clear()
            state.update(updated)
            return {
                "delivery": delivery,
                "next_ns": next_ns,
                "step": current["step"] + 1,
            }

        def restore(snapshot):
            _fields(snapshot, state)
            if (
                snapshot["format"] != state["format"]
                or type(snapshot["version"]) is not int
                or snapshot["version"] != 1
                or snapshot["population_sha256"] != digest(spec)
                or type(snapshot["started"]) is not bool
                or type(snapshot["sealed"]) is not bool
                or type(snapshot["actors"]) is not dict
                or set(snapshot["actors"]) != set(definitions)
                or type(snapshot["decisions"]) is not list
                or len(snapshot["decisions"]) > MAX_DECISIONS
            ):
                raise Fault("actor_state")
            rebuilt = {
                key: {
                    "memory": deep_copy(actor["memory"]),
                    "step": 0,
                    "rng_counter": 0,
                    "next_ns": actor["start_ns"],
                }
                for key, actor in definitions.items()
            }
            previous_ns = 0
            for index, record in enumerate(snapshot["decisions"]):
                _record(record)
                request = record["request"]
                if not previous_ns <= request["at_ns"] <= ctx.now():
                    raise Fault("actor_state")
                previous_ns = request["at_ns"]
                actor_id = request["actor_id"]
                if actor_id not in definitions:
                    raise Fault("actor_state")
                actor, row = definitions[actor_id], rebuilt[actor_id]
                if (
                    request["policy"] != actor["policy"]
                    or request["seed"] != spec["seed"]
                    or not equal(request["actions"], actor["actions"])
                    or not equal(request["profile"], actor["profile"])
                    or request["step"] != row["step"]
                    or request["at_ns"] != row["next_ns"]
                    or request["rng_before"] != row["rng_counter"]
                    or not equal(request["memory"], row["memory"])
                    or row["step"] >= actor["max_decisions"]
                ):
                    raise Fault("actor_state")
                decision = self._resolve(request, record["decision"])
                if "choices" in request and not equal(decision, record["resolution"]):
                    raise Fault("actor_choice_changed")
                if "choices" in request and decision["action"] is not None:
                    delivery = self.translate(
                        {
                            "id": actor_id,
                            "profile": deep_copy(actor["profile"]),
                            "memory": deep_copy(decision["memory"]),
                        },
                        deep_copy(decision["action"]),
                        "act_" + digest(request)[:32],
                    )
                    require_sync_result(delivery)
                    if not equal(delivery, record["delivery"]):
                        raise Fault("actor_choice_changed")
                if (
                    decision["action"] is not None
                    and decision["action"]["type"] not in actor["actions"]
                ):
                    raise Fault("actor_state")
                if (decision["action"] is None) != (record["delivery"] is None):
                    raise Fault("actor_state")
                if spec["mode"] == "replay" and (
                    index >= len(spec["tape"]) or not equal(record, spec["tape"][index])
                ):
                    raise Fault("actor_tape_mismatch")
                if spec["mode"] == "inference" and (
                    index >= len(spec["tape"])
                    or not equal(
                        {"request": request, "decision": decision}, spec["tape"][index]
                    )
                    or record["rng_after"] != request["rng_before"]
                ):
                    raise Fault("actor_tape_mismatch")
                delay = decision["wake_after_ns"]
                at_ns = None if delay is None else request["at_ns"] + delay
                if at_ns is not None and at_ns > INT64_MAX:
                    raise Fault("actor_state")
                rebuilt[actor_id] = {
                    "memory": decision["memory"],
                    "step": row["step"] + 1,
                    "rng_counter": record["rng_after"],
                    "next_ns": at_ns,
                }
            if not equal(rebuilt, snapshot["actors"]) or (
                not snapshot["started"] and snapshot["decisions"]
            ):
                raise Fault("actor_state")
            if snapshot["sealed"] and (
                not snapshot["started"]
                or any(
                    row["next_ns"] is not None and row["next_ns"] <= ctx.now()
                    for row in rebuilt.values()
                )
                or (
                    spec["mode"] in ("replay", "inference")
                    and len(snapshot["decisions"]) != len(spec["tape"])
                )
            ):
                raise Fault("actor_state")
            restored = _bounded(snapshot)
            state.clear()
            state.update(restored)

        return World({self.name: call}, lambda: deep_copy(state), restore)

    def _start(self, ctx, body):
        _fields(body, ())
        for wake in ctx.emit(self.name, {"op": "start"}):
            ctx.schedule_at(
                wake["at_ns"],
                f"{self.name}_step",
                {"actor_id": wake["actor_id"], "step": 0},
            )

    def _step(self, ctx, body):
        _fields(body, ("actor_id", "step"))
        actor = ctx.emit(self.name, {"op": "context", **body})
        observation = self.observe(deep_copy(actor), ctx.state, ctx.now())
        require_sync_result(observation)
        result = ctx.emit(
            self.name, {"op": "decide", **body, "observation": _bounded(observation)}
        )
        if result["delivery"] is not None:
            delivery = result["delivery"]
            ctx.schedule_at(ctx.now(), delivery["handler"], delivery["body"])
        if result["next_ns"] is not None:
            ctx.schedule_at(
                result["next_ns"],
                f"{self.name}_step",
                {"actor_id": body["actor_id"], "step": result["step"]},
            )

    def _finish(self, ctx, body):
        _fields(body, ())
        ctx.emit(self.name, {"op": "seal"})
