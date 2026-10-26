"""Lets `python -m assistant ...` work the same as the `assistant` command."""

import sys

from assistant.cli import main

sys.exit(main())
