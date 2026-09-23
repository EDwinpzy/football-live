"""Provider interface: the core only ever sees NormalizedMedia."""

from __future__ import annotations

from ..model import NormalizedMedia


class ProviderError(RuntimeError):
    """Raised when a provider cannot satisfy a request."""


class MetadataProvider:
    name = "base"

    def get_movie(self, external_id: str) -> NormalizedMedia | None:  # pragma: no cover - interface
        raise NotImplementedError

    def search(self, query: str) -> list[NormalizedMedia]:  # pragma: no cover - interface
        raise NotImplementedError

    def get_person(self, external_id: str):  # pragma: no cover - interface
        raise NotImplementedError

    def get_updates(self, since) -> list[NormalizedMedia]:  # pragma: no cover - interface
        raise NotImplementedError

    def collect(self, limit: int | None = None) -> list[NormalizedMedia]:  # pragma: no cover - interface
        """Bulk export used by the builder."""
        raise NotImplementedError
