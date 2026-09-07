"""The ASGI entry point uvicorn is pointed at.

Separate from `create_api` so the factory stays injectable for tests, and so the
module-level application object that a server needs exists in exactly one place.
"""

from floodline.api.app import create_api

app = create_api()
