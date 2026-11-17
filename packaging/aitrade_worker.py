"""Start file of aitrade-worker.exe (one engine process: one account, one MetaTrader 5).

It is a console-type program so that it can print its progress lines; the
window starts it hidden and reads those lines.
"""

import sys

from hedgebot.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
