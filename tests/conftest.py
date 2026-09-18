"""Sandbox-friendly default: sqlite unless DATABASE_URL is provided.

In Docker/production DATABASE_URL comes from the environment (PostgreSQL)
and setdefault() is a no-op. This only affects bare `pytest` runs where the
postgres driver may not even be installed.
"""

import os

os.environ.setdefault('DATABASE_URL', 'sqlite:////tmp/studio-tests.db')
