"""Optional external inference with durable decisions and explicit fresh-run prefix continuation."""

import fcntl
import hashlib
import multiprocessing
import os
from pathlib import Path
import platform
import sqlite3
import tempfile

from seam.actors import (
    MAX_DECISIONS,
    _bounded,
    _decision,
    _fields,
    _population,
    _request,
    _validate_selection,
    population,
)
from seam.canon import deep_copy, digest, dumps, equal, loads
from seam.case import MAX_CASE
from seam.dataset import load_datasets
from seam.runner import ProcessResult, _product_digest, run_product

MAX_MANIFEST = 8 * 1024 * 1024


class InferenceError(ValueError):
    """A refused handoff, exhausted budget, rejected response, or uncertain provider result."""


def settings(
    provider,
    model,
    prompt,
    *,
    max_calls=32,
    max_input_tokens=8192,
    max_output_tokens=1024,
    max_cost_microusd=0,
    timeout_ms=10000,
    temperature_milli=0,
    returned_models=None,
):
    """Configure one explicit provider/model; zero cost permits only zero-cost recorded or fake responses."""
    value = dict(
        provider=provider,
        model=model,
        prompt=prompt,
        max_calls=max_calls,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        max_cost_microusd=max_cost_microusd,
        timeout_ms=timeout_ms,
        temperature_milli=temperature_milli,
        returned_models=[model] if returned_models is None else returned_models,
    )
    return _settings(value)


def _settings(value):
    try:
        _fields(
            value,
            (
                "provider",
                "model",
                "prompt",
                "max_calls",
                "max_input_tokens",
                "max_output_tokens",
                "max_cost_microusd",
                "timeout_ms",
                "temperature_milli",
                "returned_models",
            ),
        )
        for key in ("provider", "model"):
            if (
                type(value[key]) is not str
                or not 1 <= len(value[key]) <= 128
                or any(ord(c) < 33 or ord(c) > 126 for c in value[key])
            ):
                raise InferenceError("bad_settings")
        if type(value["prompt"]) is not str or not 1 <= len(value["prompt"]) <= 32768:
            raise InferenceError("bad_settings")
        for key, low, high in (
            ("max_calls", 1, MAX_DECISIONS),
            ("max_input_tokens", 1, 1000000),
            ("max_output_tokens", 1, 1000000),
            ("max_cost_microusd", 0, 2**63 - 1),
            ("timeout_ms", 1, 300000),
        ):
            if type(value[key]) is not int or not low <= value[key] <= high:
                raise InferenceError("bad_settings")
        temperature = value["temperature_milli"]
        if temperature is not None and (
            type(temperature) is not int or not 0 <= temperature <= 2000
        ):
            raise InferenceError("bad_settings")
        models = value["returned_models"]
        if type(models) is not list or not 1 <= len(models) <= 16:
            raise InferenceError("bad_settings")
        if any(
            type(model) is not str or not 1 <= len(model) <= 128 for model in models
        ):
            raise InferenceError("bad_settings")
        if len(set(models)) != len(models):
            raise InferenceError("bad_settings")
        return _bounded(value)
    except InferenceError:
        raise
    except Exception:
        raise InferenceError("bad_settings") from None


def _identity(module, env):
    child = {
        key: os.environ[key] for key in ("PATH", "PYTHONPATH") if key in os.environ
    }
    child.update(PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1", TZ="UTC")
    child.update(env)
    return {
        "product": _product_digest(module, child.get("PYTHONPATH", "")),
        "sdk": _product_digest("seam", child.get("PYTHONPATH", "")),
        "python": platform.python_version(),
        "environment": digest(child),
    }


def _write(path, value):
    data = (dumps(value) + "\n").encode("ascii")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _worker(connection, provider, request, lock_fd):
    for fd in (lock_fd,) if type(lock_fd) is int else lock_fd:
        os.close(fd)
    try:
        value = provider(deep_copy(request))
        data = dumps(_bounded(value)).encode("ascii")
        connection.send_bytes(data)
    except Exception:
        connection.send_bytes(b'{"provider_error":true}')
    finally:
        connection.close()


def _call(provider, request, timeout_ms, lock_fd):
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_worker, args=(child, provider, request, lock_fd))
    try:
        process.start()
        child.close()
        if not parent.poll(timeout_ms / 1000):
            raise InferenceError("inference_timeout_uncertain")
        try:
            value = loads(parent.recv_bytes(1024 * 1024))
        except (EOFError, OSError, ValueError):
            raise InferenceError("inference_provider_uncertain") from None
        if type(value) is dict and value == {"provider_error": True}:
            raise InferenceError("inference_provider_uncertain")
        return value
    finally:
        parent.close()
        child.close()
        if process.pid is not None:
            process.join(timeout=0.1)
            if process.is_alive():
                process.terminate()
                process.join(timeout=1)
            if process.is_alive():
                process.kill()
                process.join(timeout=1)
            process.close()


class InferenceRun:
    """Own a private single-writer journal; never silently repeat a possibly sent provider request."""

    @classmethod
    def create(
        cls,
        directory,
        module,
        case_path,
        *,
        config,
        world="actors",
        env=None,
        timeout_s=None,
    ):
        env = {} if env is None else env
        if (
            type(env) is not dict
            or set(env) - {"PYTHONPATH"}
            or any(type(v) is not str for v in env.values())
        ):
            raise InferenceError("bad_environment")
        config = _settings(config)
        if timeout_s is not None and (
            type(timeout_s) is not int or not 1 <= timeout_s <= 300
        ):
            raise InferenceError("bad_execution_timeout")
        case_path = Path(case_path)
        with case_path.open("rb") as handle:
            raw = handle.read(MAX_CASE + 1)
        if len(raw) > MAX_CASE:
            raise InferenceError("case_size")
        case = loads(raw)
        if (
            type(case) is not dict
            or case.get("version") != 3
            or "resume" in case
            or "checkpoint_after" in case
        ):
            raise InferenceError("inference_requires_fresh_v3")
        if any(
            spec.get("mode") == "recording" or "tape" in spec
            for spec in case["ports"].values()
        ):
            raise InferenceError("external_tapes_unsupported")
        datasets = load_datasets(
            str(case_path), case["datasets"], case["clock"]["start_ns"]
        )
        try:
            dataset = case["worlds"][world]["dataset"]
            actors = _population(datasets[dataset])
        except (KeyError, TypeError):
            raise InferenceError("bad_actor_world") from None
        if actors["mode"] != "inference" or actors["tape"]:
            raise InferenceError("inference_requires_empty_prefix")
        if sum(value["dataset"] == dataset for value in case["worlds"].values()) != 1:
            raise InferenceError("one_inference_world_required")
        if any(
            name != dataset
            and type(value) is dict
            and value.get("format") == "seam-actors"
            and value.get("mode") == "inference"
            for name, value in datasets.items()
        ):
            raise InferenceError("one_inference_world_required")
        manifest = {
            "format": "seam-inference",
            "version": 1,
            "module": module,
            "world": world,
            "dataset": dataset,
            "case": case,
            "datasets": datasets,
            "config": config,
            "env": env,
            "identity": _identity(module, env),
            "original_case_sha256": hashlib.sha256(raw).hexdigest(),
        }
        if timeout_s is not None:
            manifest["timeout_s"] = timeout_s
        if len(dumps(manifest).encode("ascii")) > MAX_MANIFEST:
            raise InferenceError("manifest_size")
        root = Path(directory)
        root.mkdir(mode=0o700, parents=False, exist_ok=False)
        fd = os.open(
            root / "journal.sqlite", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        os.close(fd)
        result = cls(root, _creating=True)
        try:
            with result.db:
                result.db.execute(
                    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                result.db.execute(
                    "CREATE TABLE steps (n INTEGER PRIMARY KEY, request TEXT NOT NULL, state TEXT NOT NULL, response TEXT, error TEXT)"
                )
                result._set("manifest", manifest)
                result._set("manifest_sha256", digest(manifest))
            result.manifest = manifest
            fd = os.open(result.root, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            return result
        except BaseException:
            result.close()
            raise

    def __init__(self, directory, *, _creating=False):
        root = Path(directory)
        if (
            root.is_symlink()
            or not root.is_dir()
            or root.stat().st_mode & 0o077
            or root.stat().st_uid != os.getuid()
        ):
            raise InferenceError("insecure_run_directory")
        self.root = root.resolve()
        db_path = self.root / "journal.sqlite"
        if (
            db_path.is_symlink()
            or not db_path.is_file()
            or db_path.stat().st_mode & 0o077
            or db_path.stat().st_uid != os.getuid()
        ):
            raise InferenceError("insecure_journal")
        self.lock_fd = os.open(
            self.root / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.lock_fd)
            raise InferenceError("inference_writer_busy") from None
        try:
            self.db = sqlite3.connect(db_path.as_uri() + "?mode=rw", uri=True)
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA trusted_schema=OFF")
            if not _creating:
                self.manifest = self._get("manifest")
                if digest(self.manifest) != self._get("manifest_sha256"):
                    raise InferenceError("manifest_changed")
                _settings(self.manifest["config"])
                self._check_identity()
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            os.close(self.lock_fd)
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.db.close()
        os.close(self.lock_fd)

    def _get(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else loads(row[0])

    def _set(self, key, value):
        self.db.execute(
            "INSERT INTO meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, dumps(value)),
        )

    def _check_identity(self):
        if digest(self.manifest) != self._get("manifest_sha256"):
            raise InferenceError("manifest_changed")
        if not equal(
            self.manifest["identity"],
            _identity(self.manifest["module"], self.manifest["env"]),
        ):
            raise InferenceError("inference_identity_changed")

    def _steps(self):
        return self.db.execute(
            "SELECT n,request,state,response,error FROM steps ORDER BY n"
        ).fetchall()

    def _envelope(self, request, n):
        _request(request)
        config = self.manifest["config"]
        prompt = config["prompt"] + "\n" + dumps(request)
        envelope = {
            "format": "seam-inference-request",
            "version": 1,
            "run_sha256": digest(self.manifest),
            "sequence": n,
            "provider": config["provider"],
            "model": config["model"],
            "returned_models": config["returned_models"],
            "temperature_milli": config["temperature_milli"],
            "prompt": prompt,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "request": request,
            "limits": {
                key: config[key]
                for key in (
                    "max_input_tokens",
                    "max_output_tokens",
                    "max_cost_microusd",
                    "timeout_ms",
                )
            },
        }
        spent = sum(
            loads(row[0])["usage"]["cost_microusd"]
            for row in self.db.execute(
                "SELECT response FROM steps WHERE state='responded' AND n<?", (n,)
            )
        )
        envelope["limits"]["max_cost_microusd"] -= spent
        envelope["request_id"] = digest(envelope)
        return _bounded(envelope)

    def _execute(self, responses, *, tape=None):
        self._check_identity()
        root = Path(
            tempfile.mkdtemp(
                prefix="replay-" if tape is not None else "step-", dir=self.root
            )
        )
        case = deep_copy(self.manifest["case"])
        for n, (name, original) in enumerate(self.manifest["datasets"].items()):
            value = original
            if name == self.manifest["dataset"]:
                value = deep_copy(original)
                value["tape"] = responses
                if tape is not None:
                    value = population(
                        original["actors"], seed=original["seed"], tape=tape
                    )
            path = root / f"input-{n}.json"
            _write(path, value)
            case["datasets"][name]["path"] = path.name
            case["datasets"][name]["sha256"] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
        path = root / "case.json"
        _write(path, case)
        result = run_product(
            self.manifest["module"],
            str(path),
            case["namespace"],
            str(root / "artifact.json"),
            env=self.manifest["env"],
            timeout=self.manifest["timeout_s"] if "timeout_s" in self.manifest else 30,
        )
        with self.db:
            self._set(
                "last_result",
                {
                    "artifact": str(Path(result.artifact_path).relative_to(self.root)),
                    "sha256": hashlib.sha256(
                        Path(result.artifact_path).read_bytes()
                    ).hexdigest(),
                    "returncode": result.returncode,
                },
            )
        self._check_identity()
        provenance = result.artifact.get("provenance", {})
        identity = self.manifest["identity"]
        if (
            provenance.get("product", {}).get("sha256") != identity["product"]
            or provenance.get("environment_sha256") != identity["environment"]
            or provenance.get("python") != identity["python"]
        ):
            self._fail("inference_identity_changed")
        return result

    def next_request(self):
        """Run the accepted prefix in a fresh executor, or return the already journaled immutable request."""
        self._check_identity()
        failure = self._get("failure")
        if failure is not None:
            raise InferenceError(failure)
        rows = self._steps()
        responses = []
        for n, raw, state, response, error in rows:
            request = loads(raw)
            if n != len(responses) or not equal(
                request, self._envelope(request["request"], n)
            ):
                raise InferenceError("journal_changed")
            if state == "in_flight":
                if n != len(rows) - 1:
                    raise InferenceError("journal_changed")
                raise InferenceError("inference_uncertain")
            if state == "pending":
                if n != len(rows) - 1:
                    raise InferenceError("journal_changed")
                return request
            if state != "responded" or response is None:
                raise InferenceError("inference_rejected")
            value = loads(response)
            self._validate_response(request, value)
            responses.append(
                {"request": request["request"], "decision": value["decision"]}
            )
        result = self._execute(responses)
        art = result.artifact
        frame = art.get("world_states", {}).get(self.manifest["world"], {})
        accepted = frame.get("decisions", [])
        if len(accepted) != len(responses) or not equal(
            [
                {"request": row["request"], "decision": row["decision"]}
                for row in accepted
            ],
            responses,
        ):
            self._fail("inference_prefix_rejected")
        previous = self._get("accepted")
        if previous is not None and not equal(previous, accepted[: len(previous)]):
            self._fail("inference_history_changed")
        with self.db:
            self._set("accepted", accepted)
        if result.returncode == 0:
            if frame.get("sealed") is not True:
                self._fail("inference_unsealed")
            previous_digest = self._get("complete_digest")
            if previous_digest is not None and previous_digest != art["digest"]:
                self._fail("inference_history_changed")
            with self.db:
                self._set("complete_digest", art["digest"])
            return result
        fault = art.get("fault", {})
        if result.returncode != 2 or fault.get("code") != "actor_inference_required":
            self._fail("inference_executor_failed")
        if len(rows) >= self.manifest["config"]["max_calls"]:
            self._fail("inference_call_budget")
        request = loads(fault["op"])
        envelope = self._envelope(request, len(rows))
        with self.db:
            self.db.execute(
                "INSERT INTO steps VALUES (?, ?, 'pending', NULL, NULL)",
                (len(rows), dumps(envelope)),
            )
        return envelope

    def _fail(self, code):
        with self.db:
            self._set("failure", code)
        raise InferenceError(code)

    def _validate_response(self, request, response):
        try:
            _fields(
                response, ("request_id", "decision", "model", "returned_model", "usage")
            )
            _fields(
                response["usage"], ("input_tokens", "output_tokens", "cost_microusd")
            )
            config = self.manifest["config"]
            if (
                response["request_id"] != request["request_id"]
                or response["model"] != config["model"]
                or response["returned_model"] not in config["returned_models"]
            ):
                raise InferenceError("inference_response_identity")
            decision = _decision(response["decision"])
            _validate_selection(request["request"], decision)
            action = None if "choice" in decision else decision["action"]
            if (
                action is not None
                and action["type"] not in request["request"]["actions"]
            ):
                raise InferenceError("inference_action_forbidden")
            usage = response["usage"]
            for key, cap in (
                ("input_tokens", "max_input_tokens"),
                ("output_tokens", "max_output_tokens"),
                ("cost_microusd", "max_cost_microusd"),
            ):
                if type(usage[key]) is not int or not 0 <= usage[key] <= config[cap]:
                    raise InferenceError("inference_usage_budget")
            _bounded(response)
        except InferenceError:
            raise
        except Exception:
            raise InferenceError("inference_invalid_response") from None
        total = sum(
            loads(row[0])["usage"]["cost_microusd"]
            for row in self.db.execute(
                "SELECT response FROM steps WHERE state='responded' AND n<?",
                (request["sequence"],),
            )
        )
        if total + usage["cost_microusd"] > config["max_cost_microusd"]:
            raise InferenceError("inference_cost_budget")

    def accept(self, response):
        """Record one explicitly identified response, including recovery of a known result after an uncertain call."""
        self._check_identity()
        rows = self._steps()
        if not rows or rows[-1][2] not in ("pending", "in_flight"):
            raise InferenceError("inference_duplicate_response")
        n, raw, state, _, _ = rows[-1]
        request = loads(raw)
        if not equal(request, self._envelope(request["request"], n)):
            raise InferenceError("journal_changed")
        try:
            self._validate_response(request, response)
        except InferenceError as err:
            with self.db:
                self.db.execute(
                    "UPDATE steps SET state='rejected', error=? WHERE n=?",
                    (str(err), n),
                )
                self._set("failure", str(err))
            raise
        with self.db:
            self.db.execute(
                "UPDATE steps SET state='responded', response=?, error=NULL WHERE n=?",
                (dumps(response), n),
            )

    def invoke(self, provider, *, _close_fds=()):
        """Durably mark possible dispatch before calling a bounded external worker; uncertain calls are never retried."""
        request = self.next_request()
        if isinstance(request, ProcessResult):
            return request
        if not callable(provider):
            raise InferenceError("bad_provider")
        with self.db:
            changed = self.db.execute(
                "UPDATE steps SET state='in_flight' WHERE n=? AND state='pending'",
                (request["sequence"],),
            )
            if changed.rowcount != 1:
                raise InferenceError("inference_uncertain")
        try:
            response = _call(
                provider,
                request,
                self.manifest["config"]["timeout_ms"],
                (self.lock_fd, *_close_fds),
            )
        except BaseException:
            with self.db:
                self.db.execute(
                    "UPDATE steps SET error='inference_uncertain' WHERE n=?",
                    (request["sequence"],),
                )
            raise
        self.accept(response)
        return response

    def run(self, provider):
        """Continue until sealed or explicitly failed, with no provider call on an already completed trajectory."""
        while True:
            result = self.invoke(provider)
            if isinstance(result, ProcessResult):
                return result

    def replay(self):
        """Replay a completed accepted trajectory with the ordinary strict actor tape, without inference."""
        result = self.next_request()
        if not isinstance(result, ProcessResult):
            raise InferenceError("inference_incomplete")
        records = result.artifact["world_states"][self.manifest["world"]]["decisions"]
        replayed = self._execute([], tape=records)
        if (
            replayed.returncode != 0
            or not equal(
                replayed.artifact["world_states"][self.manifest["world"]]["decisions"],
                records,
            )
            or not equal(
                replayed.artifact["final_state"], result.artifact["final_state"]
            )
        ):
            raise InferenceError("inference_replay_failed")
        return replayed
