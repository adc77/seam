"""Generate a renamed tested starter into a new directory, never an existing project."""

from importlib.resources import files
from pathlib import Path
import re

from seam.canon import dumps, loads
from seam.errors import Refuse
from seam.experiments import plan

SDK_COMMIT = "ccee8cf07f19c673d607b825d624d50f0199533b"
NAME = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
MAX_DOCUMENT = 1024 * 1024
TEMPLATE_FILES = (
    ".gitignore",
    "LICENSE",
    "pyproject.toml",
    "scenario-empty.json",
    "sim_starter/__init__.py",
    "sim_starter/product.py",
    "sim_starter/inventory.py",
    "sim_starter/actors.py",
    "sim_starter/simulation.py",
    "sim_starter/experiments.py",
    "sim_starter/demo.py",
    "tests/__init__.py",
    "tests/test_starter.py",
)


class ProjectError(ValueError):
    """An actionable refusal without a reset, implicit provider, or destructive recovery."""


def identity(name):
    if type(name) is not str or not 1 <= len(name) <= 24 or not NAME.fullmatch(name):
        raise ProjectError(
            "name: use 1–24 lowercase letters/digits with single hyphens; start with a letter"
        )
    return {
        "name": name,
        "package": name.replace("-", "_") + "_sim",
        "distribution": name + "-seam-product",
        "namespace": "sim-" + name,
        "demo": name + "-demo",
    }


def manifest(name):
    names = identity(name)
    return {
        "format": "seam-project",
        "version": 1,
        "name": name,
        "package": names["package"],
        "distribution": names["distribution"],
        "module": names["package"] + ".simulation",
        "adapter": names["package"] + ".experiments",
        "namespace": names["namespace"],
        "plan": "scenario.json",
        "sdk_commit": SDK_COMMIT,
    }


def read_document(path):
    with Path(path).open("rb") as handle:
        body = handle.read(MAX_DOCUMENT + 1)
    if len(body) > MAX_DOCUMENT:
        raise ProjectError("document: maximum size is 1 MiB")
    try:
        return loads(body)
    except (ValueError, RecursionError, Refuse):
        raise ProjectError("document: invalid JSON or duplicate fields") from None


def read_project(directory):
    root = Path(directory).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ProjectError("project: expected a directory containing seam-project.json")
    value = read_document(root / "seam-project.json")
    if type(value) is not dict or type(value.get("version")) is not int:
        raise ProjectError("project: invalid manifest")
    if value != manifest(value.get("name")):
        raise ProjectError(
            "project: manifest must match the generated version-1 contract"
        )
    return root, value


def scenario(path, project):
    value = read_document(path)
    keys = {
        "format",
        "version",
        "adapter",
        "variants",
        "seeds",
        "baseline",
        "timeout_s",
    }
    if (
        type(value) is not dict
        or set(value) != keys
        or value["format"] != "seam-experiment"
        or type(value["version"]) is not int
        or value["version"] != 1
        or value["adapter"] != project["adapter"]
    ):
        raise ProjectError(
            "plan: only an explicit version-1 rule plan for this product is supported; no model calls"
        )
    return plan(
        value["adapter"],
        value["variants"],
        value["seeds"],
        baseline=value["baseline"],
        timeout_s=value["timeout_s"],
    )


def project_path(root, relative):
    if type(relative) is not str or not relative or Path(relative).is_absolute():
        raise ProjectError("path: use a file relative to the project directory")
    current = root
    for part in Path(relative).parts:
        current = current / part
        if part == ".." or current.is_symlink():
            raise ProjectError(
                "path: project files must not traverse parents or symlinks"
            )
    candidate = root / relative
    resolved = candidate.resolve(strict=True)
    if (
        not resolved.is_relative_to(root)
        or candidate.is_symlink()
        or not resolved.is_file()
    ):
        raise ProjectError(
            "path: project files must stay inside the project and not be symlinks"
        )
    return resolved


def generate(directory, name):
    names = identity(name)
    root = Path(directory).expanduser().absolute()
    replacements = (
        ("sim_starter", names["package"]),
        ("sim-starter-orders", names["namespace"]),
        ("starter-orders", name),
        ("seam-product-starter", names["distribution"]),
        ("starter-demo", names["demo"]),
    )
    documents = {}
    template = files("seam_dev").joinpath("template")
    for relative in TEMPLATE_FILES:
        body = template.joinpath(relative).read_text(encoding="utf-8")
        for before, after in replacements:
            body = body.replace(before, after)
        destination = relative.replace("sim_starter", names["package"])
        documents[destination] = body
    empty = loads(documents["scenario-empty.json"])
    specification = plan(
        manifest(name)["adapter"],
        [
            empty["variants"][0],
            {
                "name": "scarce",
                "parameters": {**empty["variants"][1]["parameters"], "capacity": 1},
            },
        ],
        empty["seeds"],
        baseline="healthy",
        timeout_s=30,
    )
    documents["scenario.json"] = dumps(specification) + "\n"
    documents["seam-project.json"] = dumps(manifest(name)) + "\n"
    documents["README.md"] = generated_readme(name)
    try:
        root.mkdir(mode=0o700, exist_ok=False)
    except OSError as error:
        raise ProjectError(
            "output: use a new directory with an existing writable parent; nothing is overwritten"
        ) from error
    try:
        for relative, body in documents.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(body)
    except OSError as error:
        raise ProjectError(
            "output: generation interrupted; partial directory retained, use a new path"
        ) from error
    return {"status": "generated", "project": str(root.resolve()), **manifest(name)}


def generated_readme(name):
    names = identity(name)
    return f"""# {name}: a synthetic Seam integration

Generated from the tested order starter, not an automatic integration of your product.
Use Python 3.11+ and Git. Keep your environment outside the system temporary directory.
All data is synthetic. The controllers are rule/scripted, not LLMs. No API key or GPU.

From this project's parent folder, in a fresh environment with seam-devtools installed:

```sh
python -m pip install ./{name}
seam-dev check --project ./{name}
mkdir runs
seam-dev run --project ./{name} --out runs/first
seam-dev view --experiment runs/first --check
```

If the directory differs from `{name}`, use its actual path. The console commands use
the active environment; run from outside the product source directory. `run` executes
the four rule-mode cells declared in `scenario.json`, not paid inference. Healthy
fulfills two orders; scarce fulfills one and rejects one per seed. Engine checks
passing does not mean every buyer achieved their goal.

`view` without `--check` serves a read-only loopback viewer; stop it with Ctrl-C.
For the separate scripted decision-protocol proof with eight offline recorded replays:

```sh
{names["demo"]} --scripted --out runs/recorded
seam-dev view --experiment runs/recorded/experiment --check
```

Change `scenario.json` for counterfactuals. Or use
`--plan scenario-empty.json` on `check` and `run` to reject both buyers.
Check stages inputs in a temporary folder and validates hashes/cutoffs. It executes
trusted product imports/preparation, not handlers, live adapters, or models. Run is
the additional proof of world-schema, authorization, timing, and outcome behavior.

The package `{names["package"]}` owns the handlers, ports, snapshots, persona
observations, legal actions, and metrics. Keep internal retries/timers out of customer
actions. This is an in-memory fixture, not a production web service or security sandbox.
The initial project manifest is deliberately narrow; this tool supports generated
order products, not arbitrary unmodified adapters or all product types.

After editing Python sources, install in a new environment with seam-devtools and the
new product. `check` refuses stale installed code. Preserve old environments/journals;
every experiment needs a new output directory. Never weaken identity checks.

Run the included product tests after installation:

```sh
cd {name}
python -m unittest discover -s tests -t . -v
```

Follow https://github.com/adc77/seam/blob/main/ADOPTING.md to connect a workflow you own.
No organization code, production exports, or credentials belong in this public fixture.
"""
