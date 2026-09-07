"""The ASGI entry point uvicorn is pointed at.

One application: the map interface at `/`, the service routes under `/api`, and
`/health` and `/ready` at the root for whatever is probing the container. The image
used to run the service factory alone, which meant the deployed artefact was missing
the only thing a visitor opens - `GET /` answered 404 in the container while working
perfectly under `floodline serve`, because those were two different applications.

Separate from the factory so it stays injectable for tests, and so the module-level
application object that a server needs exists in exactly one place.
"""

from fastapi import FastAPI

from floodline.service import create_app

app: FastAPI = create_app()
