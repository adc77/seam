import sys

from seam import main
from seam.proof.shared_store import build

sys.exit(main(build()))
