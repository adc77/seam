"""Inspect explicitly selected offline experiments through a private local browser URL."""

import argparse
import sys

from seam.canon import dumps
from seam_viewer.evidence import open_experiments, transport
from seam_viewer.server import ViewerServer


def selection(value):
    label, separator, directory = value.partition("=")
    if not separator or not label or not directory:
        raise argparse.ArgumentTypeError("use LABEL=/path/to/experiment")
    return label, directory


def main():
    parser = argparse.ArgumentParser(
        description="Read-only, loopback-only Seam experiment viewer."
    )
    parser.add_argument("--experiment", action="append", type=selection, required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify selected experiment summaries without starting HTTP.",
    )
    args = parser.parse_args()
    try:
        if args.check:
            catalog = [
                item.overview(index)
                for index, item in enumerate(open_experiments(args.experiment))
            ]
            print(dumps(transport({"experiments": catalog})))
            return 0 if all(item["available"] for item in catalog) else 1
        with ViewerServer(args.experiment, args.port) as server:
            print(f"Seam viewer: {server.url}", flush=True)
            server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError):
        print("viewer_configuration", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
