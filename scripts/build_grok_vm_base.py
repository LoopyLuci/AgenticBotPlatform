#!/usr/bin/env python
"""Build the clean base image confined Grok runs in (bot/agent_runtime/grok_vm.py): data/wsl_base/grok-base.tar.

    python scripts/build_grok_vm_base.py ROOTFS GROK_TGZ

ROOTFS is a minimal Debian rootfs (.tar or .tar.gz, e.g. the official debuerreotype trixie/slim image) and GROK_TGZ the
official `@xai-official/grok-linux-x64` npm tarball (`npm pack @xai-official/grok-linux-x64@<version>`). Verify both
before building: the npm integrity hash, and the image manifest's layer digest. Rebuild when Grok updates.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.agent_runtime import grok_vm  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    grok_vm.build_base(Path(argv[0]), Path(argv[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
