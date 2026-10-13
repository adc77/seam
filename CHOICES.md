# Product-owned action choices

Seam 0.10 adds an opt-in choice policy. Models select an intention; trusted product code
owns action parameters, memory transitions, timing gates, and follow-up observations.
Existing low-level actor policies keep their original contract.

```python
from seam.actors import ActorSystem, choice
from seam.personas import choice_prompt

def customer_choices(request):
    if request["at_ns"] < request["memory"]["ready_ns"]:
        return [choice("wait", "Wait until payment is eligible.", {
            "action": None,
            "memory": request["memory"],
            "wake_after_ns": request["memory"]["ready_ns"] - request["at_ns"],
        })]
    return make_eligible_payment_and_deferral_choices(request)

system = ActorSystem(
    {"customer_v2": deterministic_control}, observe, translate,
    choices={"customer_v2": customer_choices},
)
prompt = choice_prompt("Pay to pursue confirmation; wait defers an eligible action.")
```

The actor descriptor still declares its real action capabilities. Each registered
`choices` callback takes a detached ordinary request and returns 1–32 `choice(...)`
entries with unique IDs. Callbacks must be pure, synchronous, and deterministic from
that request alone. No closures holding mutable state, unseeded randomness, I/O, or
clocks. Put timing anchors and fixture progress in the code-owned actor memory.

The model sees `request.choices`: each entry contains `id`, `description`, `action`,
and `wake_after_ns`. It returns exactly `{"choice":"offered_id"}`. It cannot supply
an arbitrary action body, edit memory, finish outside the offered menu, or shorten
a delay. The SDK refuses invalid selections before translation or product delivery.
The inference controller also refuses them before marking a response accepted.
Providers need not implement constrained decoding; the runtime remains authoritative.

An offered action executes immediately, exactly as in legacy actors. A delay controls
the next decision, not the current action. To enforce a not-before time, offer only a
wait transition before that time, persist the eligibility anchor, and offer the action
after re-observing at or after that anchor. Every action must have a follow-up wake;
an actionless wait must advance time. Only explicit actionless terminal transitions
may have a null delay. Overflow, unauthorized actions, duplicate IDs, and malformed
menus fail closed. Actor/event budgets still bound repeated actions and deferrals.

Every choice record preserves the model's original `decision` and adds a `resolution`
with the code-owned action, resulting memory, and delay. Product metrics and viewers
must use the resolution for effects and resulting memory, and the decision for what
the model selected. Never overwrite the raw decision with a scripted policy result.

Replay re-derives the menu and resolution against the recorded request and validates
the translated delivery. Checkpoint restore re-derives choice resolutions and their
deliveries without re-running the model or rule policy. Ordinary checkpoints preserve
the next wake in the scheduler; no new in-flight action or callback state is hidden.
Legacy checkpoint restoration does not change. Exact source/environment identity
requirements remain; upgrading an existing inference journal is not supported.

`seam.proof.choices` demonstrates an independent counter with a not-before gate,
genuine action-versus-deferral choices, strict replay, and pending-wake recovery.
Glass and the private Reservations fixture independently own their domain menus.
These menus are trusted product semantics, not a security boundary against malicious
adapter code or a guarantee that a model will choose useful intentions. Report stalled
or failed goals honestly; constrained choices do not establish calibrated human behavior.
