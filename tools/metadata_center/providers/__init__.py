"""Metadata providers.  Every provider returns NormalizedMedia and nothing else."""

from .base import MetadataProvider, ProviderError
from .douban_snapshot import DoubanSnapshotProvider

__all__ = ["MetadataProvider", "ProviderError", "DoubanSnapshotProvider"]
