"""CRM adapter selection — mirrors the compute registry's rules:
configuration decides, misconfiguration fails loudly, no silent swap."""

from __future__ import annotations

from relay.config import get_settings
from relay.crm.base import CRMAdapter, CRMConfigError
from relay.logs import get_logger

log = get_logger(__name__)

_cached: CRMAdapter | None = None
_cached_kind: str | None = None


def crm_adapter() -> CRMAdapter | None:
    """The configured adapter, or None when sync is disabled."""
    global _cached, _cached_kind  # noqa: PLW0603
    kind = get_settings().crm_backend
    if kind == "none":
        return None
    if _cached is None or _cached_kind != kind:
        if kind == "memory":
            from relay.crm.memory import InMemoryCRM

            _cached = InMemoryCRM()
        elif kind == "espo":
            from relay.crm.espo import EspoCRM

            _cached = EspoCRM()
        else:
            # Fail loudly if the config Literal was extended but this
            # registry was not taught the new adapter — never silently
            # fall through to some other CRM.
            raise CRMConfigError(f"unhandled crm backend {kind!r}")
        _cached_kind = kind
        log.info("crm adapter ready", backend=kind)
    return _cached


def reset_crm() -> None:
    global _cached, _cached_kind  # noqa: PLW0603
    _cached = None
    _cached_kind = None
