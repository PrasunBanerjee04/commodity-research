"""Start the preloaded CAISO workstation: python server.py [--lake PATH]."""

import sys
from pathlib import Path

if __name__ == "__main__":
    # Allow the documented launch command directly from a source checkout.
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
    from comm_research.dashboard.server import main

    main()
