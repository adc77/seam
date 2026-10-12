"""Run actual counter experiments and inspect them through a fresh read-only HTTP process."""

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import select
import subprocess
import sys
from urllib.parse import urlsplit

import seam
import seam_viewer
from seam.canon import dumps
from seam.experiments import run_experiment
from seam.proof.experiments import demo_plan


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


def hashes(root):
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def proof(root, installed):
    if installed:
        for module in (seam, seam_viewer):
            check(
                "site-packages" in Path(module.__file__).resolve().parts,
                "proof imported checkout",
            )
    root.mkdir(mode=0o700, exist_ok=False)
    experiment = root / "counter"
    run_experiment(experiment, demo_plan())
    before = hashes(experiment)
    env = {key: os.environ[key] for key in ("PATH", "PYTHONPATH") if key in os.environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    process = subprocess.Popen(
        [sys.executable, "-m", "seam_viewer", "--experiment", f"Counter={experiment}"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready, _, _ = select.select([process.stdout], [], [], 10)
        check(bool(ready), "viewer did not start")
        line = process.stdout.readline().strip()
        check(
            line.startswith("Seam viewer: http://localhost:"), "invalid viewer startup"
        )
        url = urlsplit(line.removeprefix("Seam viewer: "))

        def get(route, method="GET"):
            connection = http.client.HTTPConnection("localhost", url.port, timeout=5)
            try:
                connection.request(method, url.path + route)
                response = connection.getresponse()
                return response.status, response.read(), dict(response.getheaders())
            finally:
                connection.close()

        for asset in ("", "app.js", "style.css", "favicon.svg"):
            status, data, headers = get(asset)
            check(status == 200 and data, "packaged viewer asset missing")
            check(
                headers["Cache-Control"] == "no-store", "viewer caches private evidence"
            )
        status, raw, _ = get("api/experiments")
        catalog = json.loads(raw)
        check(
            status == 200 and catalog["experiments"][0]["available"],
            "experiment unavailable",
        )
        check(len(catalog["experiments"][0]["runs"]) == 6, "experiment cell missing")
        status, raw, _ = get("api/experiments/0/runs/0")
        view = json.loads(raw)
        check(status == 200 and view["artifact"]["actors"], "persona evidence missing")
        check(view["artifact"]["actors"][0]["decisions"], "decision evidence missing")
        check(view["artifact"]["timeline"], "product timeline missing")
        check(get("api/experiments", "POST")[0] == 405, "viewer permits writes")
        check(get("manifest.json")[0] == 404, "viewer permits filesystem browsing")
        check(hashes(experiment) == before, "viewer changed simulation evidence")
    finally:
        process.terminate()
        _, stderr = process.communicate(timeout=5)
        check(not stderr, "viewer emitted stderr")
    summary = {
        "proof": "passed",
        "installed": installed,
        "runs": 6,
        "evidence_unchanged": True,
        "paid_calls": 0,
    }
    (root / "proof.json").write_text(dumps(summary) + "\n")
    print(dumps(summary))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--installed", action="store_true")
    args = parser.parse_args()
    proof(args.out.resolve(), args.installed)


if __name__ == "__main__":
    main()
