"""The HTTP service.

Three endpoints and no queue. The measurements that decided that are in `app`'s
docstring; the ordering constraint that makes a synchronous request affordable is in
`scenario`'s.
"""

from floodline.api.app import create_api

__all__ = ["create_api"]
