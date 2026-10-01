"""Test helpers. The parent process must never call install_guards or seam.main."""

import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASE_DIR = os.path.join(REPO, "seam", "proof", "checkout", "cases")
DECLINED = os.path.join(CASE_DIR, "declined.json")

CASE_DIGEST = "396fb7a96493c8190b8665757a8aab43d716098bc911618ddc2307870431a214"
RUN_DIGEST = "40e9b490276e08ebf25d4865628e8902521b65e83bfb1ab11139c64a8e6146bd"
GOLDEN_BODY = (
    '{"clock":{"end_ns":0,"start_ns":0},"events":[{"at_ns":0,"body":{"amount":500,'
    '"sku":"book"},"handler":"message","i":0,"kind":"deliver","status":"ok"}],'
    '"final_state":{"order":{"status":"declined"}},"namespace":"sim-checkout-declined",'
    '"port_calls":[{"at_ns":0,"during":0,"i":0,"port":"payments","request":{"amount":500},'
    '"response":{"status":"declined"},"source":"script"}],"seed":1842,'
    '"state_snapshots":[{"after":0,"state":{"order":{"status":"declined"}}}],'
    '"stop_reason":"quiescence","timers":[]}'
)
BODY_KEYS = (
    "clock",
    "events",
    "final_state",
    "namespace",
    "port_calls",
    "seed",
    "state_snapshots",
    "stop_reason",
    "timers",
)


def child_env(**values):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("SEAM_") and not key.startswith("CHECKOUT_")
    }
    env["PYTHONPATH"] = REPO
    env["PYTHONUNBUFFERED"] = "1"
    for key, value in values.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def run_proc(args, env, timeout=10):
    try:
        return subprocess.run(
            args,
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", "replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        raise AssertionError(f"timed out after {timeout}s\nstdout={stdout}\nstderr={stderr}") from None


def run_checkout(case, namespace, artifact, extra=None, timeout=10):
    env = child_env(
        SEAM_SIM="1",
        SEAM_NAMESPACE=namespace,
        SEAM_CASE=case,
        SEAM_ARTIFACT=artifact,
    )
    if extra:
        env.update(extra)
    return run_proc([sys.executable, "-m", "seam.proof.checkout"], env, timeout=timeout)


def run_script(source, env, timeout=10):
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".py")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(source)
        return run_proc([sys.executable, path], env, timeout=timeout)
    finally:
        os.remove(path)


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(obj, handle)
        handle.write("\n")


def body_of(art, *, fault=False):
    """Digest keys copied out of an artifact. Include `fault` only for a loop fault."""
    body = {key: art[key] for key in BODY_KEYS}
    if "terminal" in art:
        body["terminal"] = art["terminal"]
    if fault:
        body["fault"] = art["fault"]
    return body
