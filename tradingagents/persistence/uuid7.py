"""UUIDv7 generator (RFC 9562) for time-ordered primary keys.

UUIDv7 embeds a 48-bit millisecond Unix timestamp in the high bits, so
sequentially-generated values are monotonically increasing. That keeps
B-tree indexes append-only (no page splits on random inserts) and makes
PK ranges scan-friendly when ordered by insertion time — both wins for
hot tables like `runs`.

Resolution order (best available wins):
  1. `uuid.uuid7` — Python 3.14+ stdlib.
  2. `uuid_utils.uuid7` — present transitively via langchain-core.
  3. Native fallback per RFC 9562 §5.7.
"""

from __future__ import annotations

import os
import time
import uuid


def _native_uuid7() -> uuid.UUID:
    ts_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rnd = os.urandom(10)
    rand_a = int.from_bytes(rnd[:2], "big") & 0x0FFF        # 12 bits
    rand_b = int.from_bytes(rnd[2:10], "big") & ((1 << 62) - 1)  # 62 bits
    b = (
        ts_ms.to_bytes(6, "big")
        + ((0x7 << 12) | rand_a).to_bytes(2, "big")        # ver=7 | rand_a
        + ((0b10 << 62) | rand_b).to_bytes(8, "big")       # var=10 | rand_b
    )
    return uuid.UUID(bytes=b)


def _resolve_impl():
    stdlib = getattr(uuid, "uuid7", None)
    if callable(stdlib):
        return stdlib
    try:
        import uuid_utils  # type: ignore[import-not-found]
    except ImportError:
        return _native_uuid7
    return lambda: uuid.UUID(bytes=uuid_utils.uuid7().bytes)


_uuid7_impl = _resolve_impl()


def uuid7() -> uuid.UUID:
    """Return a new UUIDv7 as a stdlib `uuid.UUID`."""
    return _uuid7_impl()
