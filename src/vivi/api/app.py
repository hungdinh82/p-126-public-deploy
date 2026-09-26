"""Canonical ASGI entrypoint.

Implementation modules are migrated behind this stable import path so PC and
Jetson deployments never choose between two applications.
"""

from server.app import app

__all__ = ["app"]
