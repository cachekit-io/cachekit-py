#!/usr/bin/env python3
"""LAB-1140 negative proof: a target that fails to import must turn the job red.

Deliberately broken — exists only on this proof branch, never merged.
"""

from __future__ import annotations

import nonexistent_module_lab_1140  # noqa: F401  # pyright: ignore[reportMissingImports]
