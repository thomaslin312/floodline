"""The HTTP service.

Three endpoints and no queue. The measurements that decided that are in `app`'s
docstring; the ordering constraint that makes a synchronous request affordable is in
`scenario`'s.

`attach_api` puts these routes on the application that also serves the map, which is
what the container runs. `create_api` builds them alone, for tests that want the
service without the page.
"""

from floodline.api.app import attach_api, create_api

__all__ = ["attach_api", "create_api"]
