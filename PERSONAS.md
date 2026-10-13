# Goal-driven personas

`seam.personas` describes synthetic goals without choosing a model or changing actor execution.
Use `persona(name, goals, constraints=...)` inside a product-approved `profile.persona` field.
Use `goal_prompt(product_contract)` as an ordinary `seam.inference.settings` prompt.
For models, prefer [`choice_prompt(product_contract)` with product-owned choices](CHOICES.md).
This opt-in contract prevents models from inventing action parameters, editing memory,
or controlling timing; the legacy goal prompt remains available for existing policies.
The product contract describes action semantics, ownership, memory shape, units, and terminal outcomes.
It must not supply the expected decision or a scripted transition table.

The profile is part of every recorded actor request. The compiled prompt is part of immutable
inference settings. Existing journals, limits, actor capabilities, product validation, and
strict offline replay still apply. Any compatible provider callable can execute the same policy;
no provider SDK is imported by this module. Explicit synthetic provenance is required.

Models can choose differently from rule policies. Evaluate actual product outcomes separately
from engine completion, and report failed goals, unfinished actors, wasted capacity, retries,
and uncertainty. Never certify a goal solely because an actor writes `phase=done`.
Preserve rule-mode regression controls, paired populations, and frozen seeds/acceptance criteria.
Record raw model responses privately; replay accepted decisions without calling a model again.

Prose does not grant permissions. Product adapters must validate the persona field and preserve
ownership checks. This is not a security sandbox for malicious product code, a calibrated human
model, or evidence that every model/provider follows these goals reliably.
