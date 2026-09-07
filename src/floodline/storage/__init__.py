"""Where computed terrain is kept between requests.

An interface and one implementation. The interface exists because the filesystem is
the right answer for a single machine and the wrong one for several, and the shape of
the replacement - object storage, most likely - is already visible from the three
methods here. The implementation is local only, because writing the second one before
anyone needs it is how an abstraction gets fitted to a guess.
"""

from floodline.storage.base import TerrainArtifact, TerrainStore, params_hash
from floodline.storage.local import LocalTerrainStore

__all__ = ["LocalTerrainStore", "TerrainArtifact", "TerrainStore", "params_hash"]
