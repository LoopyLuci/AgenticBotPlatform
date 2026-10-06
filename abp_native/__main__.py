"""`python -m abp_native` - which implementation of each fast path is live in this process."""
from __future__ import annotations

import sys

from abp_native import active, describe


def main() -> int:
    print(f"abp_native: {active()}")
    for line in describe():
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
