"""Run the test suite, and refuse to pass on a run that tested nothing.

`python -m unittest discover` exits 0 when it discovers no tests at all, so a
renamed, moved or unimportable test file would leave CI green while checking
nothing. A green run that proves nothing is exactly the failure mode this
repo already paid for once, so the count is asserted explicitly.

    python run_tests.py
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PATTERN = "test_*.py"

# Guards against the suite vanishing, not against it shrinking. Raise it to a
# real watermark only if you want CI to also police the suite's size.
MIN_TESTS = 1


def main() -> int:
    suite = unittest.TestLoader().discover(HERE, pattern=PATTERN, top_level_dir=HERE)
    result = unittest.TextTestRunner(verbosity=2).run(suite)

    if result.testsRun < MIN_TESTS:
        print(f"::error::Discovered {result.testsRun} tests in {HERE} matching "
              f"{PATTERN}, expected at least {MIN_TESTS}. A green run that "
              f"tested nothing is not a pass.", file=sys.stderr)
        return 1

    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
