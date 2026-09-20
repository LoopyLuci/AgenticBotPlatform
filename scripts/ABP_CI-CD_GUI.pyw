"""ABP CI-CD Dashboard launcher.

Double-click on Windows (a .pyw runs without a console window), or run
`python scripts/ABP_CI-CD_GUI.pyw`. Options are the same as
`python -m abp_cicd.gui --help` (e.g. --source http --url http://server:8787 --token ...).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from abp_cicd.gui import main  # noqa: E402

sys.exit(main())
