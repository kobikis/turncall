"""The `turncall` command line (#77).

Exists for one reason above the others: a pull request can be gated on agent
behaviour only if something exits non-zero when the agent regressed. Everything
else here is in service of that exit code.

An ordinary API client, not an in-process caller — same key, same endpoints, no
privileged path. `argparse` rather than a CLI framework: three subcommands and
a dozen flags is what the standard library is for, and a dependency the API
image would carry for nothing is a poor trade.
"""

# Deliberately re-exporting `run` and not `main`: the console-script entry
# point is `main:run`, and binding the name `main` here would shadow the
# `turncall.cli.main` submodule for anything importing it.
from turncall.cli.main import run

__all__ = ["run"]
