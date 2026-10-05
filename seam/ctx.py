"""The handler surface. Live and sim hosts both sit behind this object."""

from seam.canon import INT64_MAX, MAX_STRING, deep_copy, dumps
from seam.errors import Fault

#: A state snapshot is canonicalised to one string before it is measured, so
#: it is bounded by the same limit as any other string.
MAX_STATE = MAX_STRING


def _check_size(value):
    if len(dumps(value).encode("ascii")) > MAX_STATE:
        raise Fault("bad_value")


def _index(seg, length):
    if type(seg) is not str or not seg.isdigit():
        raise Fault("bad_value")
    i = int(seg)
    if str(i) != seg or i < 0 or i >= length:
        raise Fault("bad_value")
    return i


def _require(parent, seg):
    if type(parent) is dict:
        if seg not in parent:
            raise Fault("bad_value")
        return parent[seg]
    if type(parent) is list:
        return parent[_index(seg, len(parent))]
    raise Fault("bad_value")


def _assign(parent, seg, value):
    if type(parent) is dict:
        parent[seg] = value
        return
    if type(parent) is list:
        parent[_index(seg, len(parent))] = value
        return
    raise Fault("bad_value")


def replace_at(state, path, value):
    """Set `path` on a copy of `state`. `""` replaces the whole value. Missing parents fault."""
    value = deep_copy(value)
    if path == "":
        _check_size(value)
        return value
    if type(path) is not str or not path or path.startswith(".") or path.endswith(".") or ".." in path:
        raise Fault("bad_value")
    parts = path.split(".")
    root = deep_copy(state)
    if len(parts) == 1:
        _assign(root, parts[0], value)
    else:
        cur = root
        for seg in parts[:-1]:
            cur = _require(cur, seg)
        _assign(cur, parts[-1], value)
    _check_size(root)
    return root


class Ctx:
    def __init__(self, host, handler):
        self._host = host
        self._handler = handler

    def now(self):
        return self._host.now()

    def utc(self):
        return self._host.utc()

    def rand_u64(self):
        return self._host.rand_u64()

    def rand_below(self, n):
        return self._host.rand_below(n)

    def id(self, prefix):
        return self._host.ident(prefix)

    def emit(self, port, request):
        return self._host.emit(port, request)

    def schedule_after(self, delay_ns, handler, body, name=None):
        return self._host.schedule_after(delay_ns, handler, body, name=name)

    def schedule_at(self, at_ns, handler, body, name=None):
        return self._host.schedule_at(at_ns, handler, body, name=name)

    def cancel(self, token):
        self._host.cancel(token)

    def stop(self, name):
        self._host.stop(name)

    def set_state(self, value):
        self._host.set_state(value)

    def patch(self, path, value):
        self._host.patch(path, value)

    def stamp(self, row):
        return self._host.stamp(row)

    @property
    def state(self):
        return self._host.state_copy()

    @property
    def namespace(self):
        return self._host.namespace

    @property
    def handler(self):
        return self._handler

    @property
    def config(self):
        return deep_copy(self._host.config)


def assert_int(value, *, minimum=None, maximum=INT64_MAX):
    if type(value) is not int:
        raise Fault("bad_value")
    if minimum is not None and value < minimum:
        raise Fault("bad_value")
    if maximum is not None and value > maximum:
        raise Fault("bad_value")
    return value
