"""Wire actors onto the ordinary product handlers only in the supervised simulation entry point."""

import sys

from seam import main
from sim_starter.actors import build

if __name__ == "__main__":
    sys.exit(main(build()))
