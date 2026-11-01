"""Compatibility module for the portable task runtime implementation."""

import sys

from ..task_runtime import execution as _implementation

# One actual module: patches, class identity and connection pools stay shared.
sys.modules[__name__] = _implementation
