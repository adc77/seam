# seam

Working title. A Python library that ships inside a product and stays quiet there, and that runs the same product in a separate seeded process against a scripted or recorded outside world.

The spec is [PLAN.md](PLAN.md). The license is not chosen. This repository is local; it is not published.

```shell
python3 -m unittest discover -s tests -t .
```

Run that from the repository root. The suite is in-process unit tests plus short subprocesses. Nothing in v1 needs a network, a GPU, or another machine.
