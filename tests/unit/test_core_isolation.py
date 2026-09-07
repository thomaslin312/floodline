"""core/ must not reach outside itself. Checked, not trusted.

The whole point of the boundary is that it holds when nobody is looking at it. A single
convenience import of a fetcher into a modelling module would make the model
untestable without a network, unusable in a worker with no disk, and impossible to
reason about without tracing where a path came from - and it would do all that without
breaking a single existing test. So the boundary gets its own.
"""

from __future__ import annotations

import ast
import pathlib

CORE = pathlib.Path("src/floodline/core")
ALLOWED_OUTSIDE_CORE = {"floodline.settings"}


def _imports(path: pathlib.Path) -> set[str]:
    """Every module this file imports, at any indentation."""
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
    return found


def test_core_imports_nothing_outside_core_and_settings() -> None:
    offenders: list[str] = []
    for path in sorted(CORE.rglob("*.py")):
        for module in sorted(_imports(path)):
            if not module.startswith("floodline"):
                continue  # third-party numerics are fine; this rule is about our own layers
            if module.startswith("floodline.core"):
                continue
            if module in ALLOWED_OUTSIDE_CORE:
                continue
            offenders.append(f"{path.relative_to('src')} imports {module}")
    assert not offenders, "core reached outside itself:\n  " + "\n  ".join(offenders)


def test_core_opens_no_sockets() -> None:
    """A modelling module that can make a request is one that can fail on a network."""
    network = {"httpx", "requests", "urllib", "urllib.request", "aiohttp", "socket", "boto3"}
    offenders = [
        f"{path.relative_to('src')} imports {module}"
        for path in sorted(CORE.rglob("*.py"))
        for module in sorted(_imports(path))
        if module.split(".")[0] in network
    ]
    assert not offenders, "core can reach the network:\n  " + "\n  ".join(offenders)


def test_core_holds_no_hardcoded_urls() -> None:
    """An endpoint in core is a deployment detail that escaped into the model."""
    offenders = []
    for path in sorted(CORE.rglob("*.py")):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith(("#", "*")) or '"""' in stripped:
                continue  # prose may cite a source; only code is the concern
            if "http://" in line or "https://" in line:
                offenders.append(f"{path.relative_to('src')}:{number}: {stripped[:70]}")
    assert not offenders, "core carries a URL:\n  " + "\n  ".join(offenders)


SRC = pathlib.Path("src/floodline")


def test_every_http_client_carries_a_timeout() -> None:
    """An httpx client built without a timeout waits forever by default.

    Upstream degradation was the most common failure in this project's development:
    the elevation API, the boundary service, the object store and FEMA's endpoint were
    each unreachable or rate-limiting at some point in one week. A request that hangs
    holds a worker slot until something else gives up, which turns one slow agency into
    an outage here.

    Checked structurally rather than by convention, because the failure is invisible in
    review: the call that hangs looks exactly like the call that does not.
    """
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if "migrations" in path.parts:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            is_client = (
                isinstance(target, ast.Attribute)
                and target.attr == "Client"
                and isinstance(target.value, ast.Name)
                and target.value.id == "httpx"
            )
            if not is_client:
                continue
            if not any(keyword.arg == "timeout" for keyword in node.keywords):
                offenders.append(f"{path.relative_to('src')}:{node.lineno}")
    assert not offenders, (
        "httpx.Client built without a timeout, which waits forever:\n  "
        + "\n  ".join(offenders)
        + "\nUse floodline.io.sources.make_client, which always sets one."
    )


def test_gdal_range_reads_are_bounded() -> None:
    """The elevation read is the one upstream call that does not go through httpx.

    GDAL's defaults are unbounded too, and a black-holed tile host would otherwise
    hang the whole request inside rasterio where no Python timeout reaches it.
    """
    from floodline.compute import VSICURL_ENV

    for key in ("GDAL_HTTP_TIMEOUT", "GDAL_HTTP_CONNECTTIMEOUT", "GDAL_HTTP_MAX_RETRY"):
        assert key in VSICURL_ENV, f"{key} is unset, so a stalled tile read has no bound"
        assert int(VSICURL_ENV[key]) > 0  # type: ignore[arg-type]
