"""Process bootstrap helpers for the operator-run entrypoints.

pydantic-settings reads ``.env`` into the ``Settings`` object, but NOT into
``os.environ``. Tools that consult the real process environment — notably
boto3's default credential chain (``AWS_ACCESS_KEY_ID`` /
``AWS_SECRET_ACCESS_KEY`` / ``AWS_REGION``) — therefore do not see values
that live only in ``.env``.

The worker and poller entrypoints call :func:`load_local_dotenv` so a
local pilot can keep AWS credentials in ``.env`` and have boto3 pick them
up. It NEVER overrides a variable already present in the real environment,
so a deployment that injects credentials the normal way (systemd
``EnvironmentFile``, container env, CI secrets) always wins over the file.
RELAY itself never reads or stores the AWS secret — it only ensures the
environment is populated for boto3.
"""

from __future__ import annotations

import os
from pathlib import Path

from relay.logs import get_logger

log = get_logger(__name__)


def load_local_dotenv(path: str = ".env") -> int:
    """Populate ``os.environ`` from a local ``.env`` (real env wins).

    Returns the number of keys injected. Silent no-op if the file is
    absent. Values are read but never logged.
    """
    p = Path(path)
    if not p.exists():
        return 0
    injected = 0
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            injected += 1
    if injected:
        log.info("loaded local .env into environment", keys=injected, path=str(p))
    return injected
