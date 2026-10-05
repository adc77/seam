import sys

from seam import main
from seam.proof.inventory import build


if __name__ == "__main__":
    sys.exit(main(build()))
