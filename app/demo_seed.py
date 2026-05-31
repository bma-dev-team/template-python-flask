"""Per-build demo staging hook. REPLACE this stub in each build.

stage_demo() stages a complete in-progress run into session and/or output dirs
(running the build's REAL write engines so staged outputs are genuine), then
returns the URL to redirect to so the operator lands on a populated screen.
The template ships a no-op that redirects to /health; bma-execute-build init
scaffolds a build-shaped version.
"""
from __future__ import annotations


def stage_demo() -> str:
    """Stage mock state and return the entry redirect URL. Override per build."""
    return "/health"
