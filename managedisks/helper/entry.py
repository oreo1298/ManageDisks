"""Stand-alone entry point used when running from a source checkout:

    pkexec /usr/bin/python3 -I /path/to/managedisks/helper/entry.py

It makes the checkout importable even though pkexec clears the environment.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from managedisks.helper.server import main  # noqa: E402

sys.exit(main())
