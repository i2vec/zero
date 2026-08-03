"""Resource cache: where Labwright's real-time collection results are pinned.

MVP has no pre-built resource library; Labwright collects from the public
internet on demand and drops the result here. The cache layout mirrors the
in-sandbox mount points, so it gradually grows into a reusable library.
"""

from zero.resources.cache import CachedResource, ResourceCache

__all__ = ["CachedResource", "ResourceCache"]
