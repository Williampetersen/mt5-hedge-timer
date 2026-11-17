"""Entry point: the window by default, or one engine worker with `worker ...`."""

from __future__ import annotations

import sys
from typing import Optional

WORKER_COMMAND = "worker"
SELFTEST_COMMAND = "--selftest"  # --selftest <report.json>: check that the installed program is complete
UNINSTALL_COMMAND = "--uninstall"  # what "Installed apps" in Windows calls to remove Aitrade


def main(argv: Optional[list[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == WORKER_COMMAND:
        # Same program, engine mode: this is how the window starts the BUY and SELL processes.
        from hedgebot.__main__ import main as worker_main

        return worker_main(args[1:])
    if args and args[0] == SELFTEST_COMMAND:
        from aitrade.selftest import run_selftest

        return run_selftest(args[1] if len(args) > 1 else "")
    if args and args[0] == UNINSTALL_COMMAND:
        from aitrade.ui.uninstall import run_uninstall

        return run_uninstall(silent="--silent" in args)
    from aitrade.ui.app import run

    return run(args)


if __name__ == "__main__":
    sys.exit(main())
