"""Product-owned simulation backends initialized from pinned JSON exports."""

from dataclasses import dataclass

from seam.canon import deep_copy
from seam.errors import Fault, Refuse


@dataclass(frozen=True)
class Backend:
    """A request handler and a JSON snapshot of its current state."""

    handle: object
    snapshot: object

    def __post_init__(self):
        if not callable(self.handle) or not callable(self.snapshot):
            raise Refuse("bad_backend")


class BackendPort:
    source = "backend"

    def __init__(self, factory, data, config):
        self.factory = factory
        self.data = data
        self.config = config
        self.client = None

    def call(self, request):
        from seam.runtime import require_sync, require_sync_result

        if self.client is None:
            client = self.factory(deep_copy(self.data), deep_copy(self.config))
            require_sync_result(client)
            if not isinstance(client, Backend):
                raise Fault("bad_backend")
            require_sync(client.handle, fault=True)
            require_sync(client.snapshot, fault=True)
            self.client = client
        result = self.client.handle(request)
        require_sync_result(result)
        return deep_copy(result)

    def state(self):
        if self.client is None:
            return deep_copy(self.data)
        from seam.runtime import require_sync_result

        value = self.client.snapshot()
        require_sync_result(value)
        return deep_copy(value)
