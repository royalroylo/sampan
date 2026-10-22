"""`python -m sampan` runs the same entry point as the `sampan` command."""
import sys

from .cli import main

sys.exit(main())
