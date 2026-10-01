import os
import sys

from seam import in_sim, main
from seam.canon import loads
from seam.proof.checkout import build


def entry():
    rt = build()
    if in_sim():
        return main(rt)
    rt.start_live()
    raw = os.environ.get("CHECKOUT_BODY")
    if raw:
        rt.deliver("message", loads(raw))
    return 0


if __name__ == "__main__":
    sys.exit(entry())
