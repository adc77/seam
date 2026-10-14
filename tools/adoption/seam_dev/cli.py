"""Explicit scaffold, preflight, rule run, and read-only viewer commands; no provider discovery."""

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from seam.canon import dumps

from seam_dev.project import ProjectError, generate


def worker(command, project, *, plan=None, out=None):
    arguments = [
        sys.executable,
        "-I",
        "-m",
        "seam_dev.worker",
        command,
        "--project",
        str(Path(project).expanduser().resolve()),
    ]
    if plan is not None:
        arguments.extend(["--plan", plan])
    if out is not None:
        output = Path(out).expanduser().absolute()
        if output.exists() or output.is_symlink() or not output.parent.is_dir():
            raise ProjectError(
                "output: use a new directory with an existing writable parent; existing evidence is preserved"
            )
        arguments.extend(["--out", str(output)])
    environment = {
        "PATH": os.environ["PATH"],
        "TZ": "UTC",
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    with tempfile.TemporaryDirectory(prefix="seam-dev-controller-") as directory:
        return subprocess.run(
            arguments, cwd=directory, env=environment, check=False
        ).returncode


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser(
        "init", help="generate the tested synthetic order template into a new directory"
    )
    init.add_argument("directory", type=Path)
    init.add_argument("--name", required=True)
    for name in ("check", "run"):
        command = commands.add_parser(name)
        command.add_argument("--project", required=True, type=Path)
        command.add_argument(
            "--plan", help="rule-mode plan relative to the project directory"
        )
        if name == "run":
            command.add_argument("--out", required=True, type=Path)
    viewer = commands.add_parser(
        "view", help="inspect retained evidence without writes or model calls"
    )
    viewer.add_argument("--experiment", required=True, type=Path)
    viewer.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            print(dumps(generate(args.directory, args.name)))
            return 0
        if args.command in ("check", "run"):
            return worker(
                args.command,
                args.project,
                plan=args.plan,
                out=args.out if args.command == "run" else None,
            )
        arguments = [
            sys.executable,
            "-I",
            "-m",
            "seam_viewer",
            "--experiment",
            f"Product={args.experiment.expanduser().resolve()}",
        ]
        arguments.extend(["--check"] if args.check else ["--port", "0"])
        return subprocess.run(arguments, check=False).returncode
    except (ProjectError, OSError) as error:
        print(f"seam-dev: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
