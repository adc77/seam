"""Run-scoped dependency worlds shared by explicitly registered product ports."""

from dataclasses import dataclass
from collections.abc import Callable

from seam.canon import deep_copy, equal
from seam.errors import Fault, PortError, Refuse
from seam.rng import Rng


@dataclass(frozen=True)
class World:
    """A shared set of synchronous port handlers with snapshot, restore, and optional cleanup."""

    handlers: dict[str, Callable[[object], object]]
    snapshot: Callable[[], object]
    restore: Callable[[object], None]
    close: Callable[[], None] | None = None

    def __post_init__(self):
        if (
            type(self.handlers) is not dict
            or any(type(name) is not str or not callable(fn) for name, fn in self.handlers.items())
            or not callable(self.snapshot)
            or not callable(self.restore)
            or (self.close is not None and not callable(self.close))
        ):
            raise Fault("bad_backend")


class WorldContext:
    """Dependency access to the run's clock, randomness, namespace, and copied config."""

    def __init__(self, host):
        self._host = host
        self._bootstrap = None
        self._rng = None
        self._frozen = False

    def now(self):
        return self._host.now() if self._bootstrap is None else self._bootstrap["at_ns"]

    def utc(self):
        from seam.clock import format_utc

        return format_utc(self.now())

    def rand_u64(self):
        if self._frozen:
            raise Fault("bad_backend", op="world.lifecycle_rng")
        return self._host.rand_u64() if self._rng is None else self._rng.rand_u64()

    def rand_below(self, n):
        if self._frozen:
            raise Fault("bad_backend", op="world.lifecycle_rng")
        return self._host.rand_below(n) if self._rng is None else self._rng.rand_below(n)

    def id(self, prefix):
        from seam.canon import ID_HEX_WIDTH
        from seam.case import IDENT

        if type(prefix) is not str or not IDENT.fullmatch(prefix):
            raise Fault("bad_value")
        return f"{prefix}_{self.rand_u64():0{ID_HEX_WIDTH}x}"

    @property
    def namespace(self):
        return self._host.namespace

    @property
    def config(self):
        return deep_copy(self._host.config)


class WorldManager:
    def __init__(self, factory, ports, data, config):
        self.factory = factory
        self.ports = frozenset(ports)
        self.data = data
        self.config = config
        self.client = None
        self.context = None
        self.bootstrap = None
        self.closed = False

    def bind(self, host):
        self.context = WorldContext(host)

    def _load(self, bootstrap=None):
        from seam.runtime import require_sync, require_sync_result

        if self.closed or self.context is None:
            raise Fault("bad_backend")
        if self.client is not None:
            return
        if bootstrap is None:
            bootstrap = {"at_ns": self.context.now(), "rng_counter": self.context._host.rng.counter}
        else:
            self.context._bootstrap = bootstrap
            self.context._rng = Rng(self.context._host.case.seed)
            self.context._rng.counter = bootstrap["rng_counter"]
        try:
            client = self.factory(self.context, deep_copy(self.data), deep_copy(self.config))
            require_sync_result(client)
            if not isinstance(client, World):
                raise Fault("bad_backend")
            self.client = client
            if set(client.handlers) != self.ports:
                raise Fault("bad_backend")
            for fn in (*client.handlers.values(), client.snapshot, client.restore):
                require_sync(fn, fault=True)
            if client.close is not None:
                require_sync(client.close, fault=True)
            self.bootstrap = deep_copy(bootstrap)
        finally:
            self.context._bootstrap = None
            self.context._rng = None

    def call(self, port, request):
        from seam.runtime import require_sync_result

        try:
            self._load()
            response = self.client.handlers[port](request)
            require_sync_result(response)
            return deep_copy(response)
        except (Fault, PortError):
            raise
        except Refuse as err:
            raise Fault(err.code, op=err.op, exc_type=err.exc_type) from None
        except BaseException as err:
            raise Fault("bad_backend", exc_type=type(err).__name__) from None

    def _frozen_call(self, fn, *args):
        from seam.runtime import require_sync_result

        previous = self.context._frozen
        self.context._frozen = True
        try:
            value = fn(*args)
            require_sync_result(value)
            return value
        finally:
            self.context._frozen = previous

    def state(self):
        if self.client is None:
            return deep_copy(self.data)
        return deep_copy(self._frozen_call(self.client.snapshot))

    def checkpoint(self, snapshot):
        if self.client is None:
            return {"initialized": False}
        return {"initialized": True, "bootstrap": deep_copy(self.bootstrap), "snapshot": snapshot}

    def restore(self, frame):
        if not frame["initialized"]:
            return
        self._load(frame["bootstrap"])
        if self._frozen_call(self.client.restore, deep_copy(frame["snapshot"])) is not None:
            raise Fault("bad_backend", op="world.restore_result")
        if not equal(self.state(), frame["snapshot"]):
            raise Fault("bad_backend", op="world.restore_state")

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.client is not None and self.client.close is not None:
            if self._frozen_call(self.client.close) is not None:
                raise Fault("bad_backend", op="world.close_result")


class WorldPort:
    source = "world"

    def __init__(self, name, manager):
        self.name = name
        self.manager = manager

    def call(self, request):
        return self.manager.call(self.name, request)
