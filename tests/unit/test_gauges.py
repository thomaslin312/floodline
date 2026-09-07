from __future__ import annotations

from typing import Any

import httpx
import pytest

from floodline.core.config import Config
from floodline.io.sources import CFS_TO_CMS, FetchContext, find_gauges, peak_discharge

RDB_SITES = """# comment
agency_cd\tsite_no\tstation_nm\tdec_lat_va\tdec_long_va\tdrain_area_va
5s\t15s\t50s\t16s\t16s\t8s
USGS\t08074500\tWhiteoak Bayou at Houston, TX\t29.7752\t-95.3972\t95.0
USGS\t08074000\tBuffalo Bayou at Houston, TX\t29.7602\t-95.4086\t336.0
"""

RDB_PEAKS = """# comment
agency_cd\tsite_no\tpeak_dt\tpeak_va
5s\t15s\t10d\t8s
USGS\t08074500\t1935-12-09\t14800
USGS\t08074500\t2017-08-27\t50600
USGS\t08074500\t2001-06-09\t\t
"""


def context(handler: Any, tmp_path: Any) -> FetchContext:
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://example.test")
    config = Config.model_validate({"sources": {"max_attempts": 1, "backoff_seconds": 0.0}})
    return FetchContext(config=config, dest=tmp_path, client=client)


def test_find_gauges_parses_the_rdb_table(tmp_path: Any) -> None:
    ctx = context(lambda r: httpx.Response(200, text=RDB_SITES), tmp_path)
    sites = find_gauges(ctx, (-95.5, 29.7, -95.3, 29.8))
    assert [s["site_no"] for s in sites] == ["08074500", "08074000"]
    assert sites[1]["drain_area_va"] == "336.0"


def test_find_gauges_returns_nothing_rather_than_failing_on_404(tmp_path: Any) -> None:
    """NWIS answers an empty bbox with 404, which is a normal result, not an error."""
    ctx = context(lambda r: httpx.Response(404), tmp_path)
    assert find_gauges(ctx, (0.0, 0.0, 1.0, 1.0)) == []


def test_peak_discharge_takes_the_largest_and_converts_units(tmp_path: Any) -> None:
    ctx = context(lambda r: httpx.Response(200, text=RDB_PEAKS), tmp_path)
    peak = peak_discharge(ctx, "08074500")
    assert peak is not None
    assert peak["discharge_cfs"] == 50600
    assert peak["discharge_cms"] == pytest.approx(50600 * CFS_TO_CMS)
    assert peak["date"] == "2017-08-27"


def test_peak_discharge_skips_rows_with_no_value(tmp_path: Any) -> None:
    """A gauge-height-only year has a blank peak_va and must not be counted."""
    ctx = context(lambda r: httpx.Response(200, text=RDB_PEAKS), tmp_path)
    peak = peak_discharge(ctx, "08074500")
    assert peak is not None
    assert peak["n_years"] == 2


def test_peak_discharge_returns_the_whole_series(tmp_path: Any) -> None:
    """The series is what lets a caller pick the peak belonging to a given flood."""
    ctx = context(lambda r: httpx.Response(200, text=RDB_PEAKS), tmp_path)
    peak = peak_discharge(ctx, "08074500")
    assert peak is not None
    years = {entry["date"][:4] for entry in peak["series"]}
    assert years == {"1935", "2017"}


def test_peak_discharge_is_none_where_there_is_no_record(tmp_path: Any) -> None:
    ctx = context(lambda r: httpx.Response(200, text="# nothing\na\tb\n5s\t5s\n"), tmp_path)
    assert peak_discharge(ctx, "99999999") is None


def test_a_malformed_row_is_skipped_not_fatal(tmp_path: Any) -> None:
    broken = RDB_SITES + "USGS\ttoo\tfew\n"
    ctx = context(lambda r: httpx.Response(200, text=broken), tmp_path)
    assert len(find_gauges(ctx, (-95.5, 29.7, -95.3, 29.8))) == 2
