"""Dashboard API over a store filled with synthetic data."""
import datetime as dt

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from miaband import pipeline, store  # noqa: E402
from miaband.config import Config  # noqa: E402
from miaband.sources.synthetic import Scenario, generate  # noqa: E402
from miaband.web.app import create_app  # noqa: E402


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    path = tmp_path_factory.mktemp("web") / "s.sqlite"
    con = store.connect(path)
    batch, _ = generate(Scenario(first_wake=dt.date(2026, 6, 1), nights=40,
                                 missing_nights={dt.date(2026, 6, 30)},
                                 workouts={dt.date(2026, 6, 20): (dt.time(18), 90, 150)}))
    store.write_batch(con, batch)
    pipeline.run(con, Config())
    con.close()
    return TestClient(create_app(path))


def test_range_and_page(client):
    r = client.get("/api/range").json()
    assert r["min"] <= r["default"] <= r["max"]
    page = client.get("/")
    assert page.status_code == 200 and "chart.umd.js" in page.text
    assert client.get("/static/chart.umd.js").status_code == 200


def test_day_carries_status_and_reason(client):
    ok = client.get("/api/day/2026-06-25").json()["metrics"]
    assert ok["recovery.score"]["status"] == "ok" and ok["readiness.rule"]["reason"]
    gap = client.get("/api/day/2026-06-30").json()["metrics"]
    assert gap["sleep.tst_min"]["status"] == "insufficient_data" and gap["sleep.tst_min"]["value"] is None
    assert client.get("/api/day/2030-01-01").status_code == 404
    assert client.get("/api/day/not-a-date").status_code == 422


def test_series_window(client):
    s = client.get("/api/series", params={"end": "2026-06-25", "days": 28}).json()
    assert len(s["dates"]) == 28 and s["dates"][-1] == "2026-06-25"
    assert s["series"]["strain.load"]["value"][s["dates"].index("2026-06-20")] == 270
