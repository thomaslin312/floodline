"""The model: arrays and parameters in, arrays and numbers out.

Everything under `core` is pure computation. It reads no files, opens no sockets, and
knows nothing about a cache, a request, or a filesystem layout. The one import it is
allowed outside itself is `floodline.settings`, and in practice it does not need even
that - a core function that wanted a URL would be a sign the boundary had moved.

That constraint is what makes the modelling half testable without a network, runnable
inside a worker that has no disk, and reviewable without tracing where a path came
from. It is checked rather than trusted: `tests/unit/test_core_isolation.py` walks
every import in this package and fails if one points outside.

Three domains, in the order the pipeline runs them:

* `terrain` - conditioning, flow routing, stream extraction, height above nearest
  drainage. Expensive, and depends on nothing but the elevation grid and parameters.
* `hydro` - a discharge becomes a per-reach stage, and a stage becomes a depth. Cheap,
  and depends on the terrain above it.
* `damage` and `exposure` - what the water reaches and what it costs, with the Monte
  Carlo that puts an interval around it.

The split between the first and the second is the one that matters for deployment, and
`floodline.pipeline` makes it explicit.
"""
