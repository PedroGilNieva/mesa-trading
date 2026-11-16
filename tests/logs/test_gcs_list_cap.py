"""El listado con tope no recorre el prefijo: se queda con los nombres finales."""

from urllib.parse import parse_qs, urlparse

from mesa_trading.gcs import _list


def _fetch_factory(objects: list[str], calls: list[str]):
    def fetch(url: str) -> dict:
        calls.append(url)
        if "pageToken" in url:
            raise AssertionError(url)
        query = parse_qs(urlparse(url).query)
        start = query.get("startOffset", [None])[0]
        limit = int(query["maxResults"][0])
        pool = [name for name in objects if start is None or name >= start]
        page = pool[:limit]
        token = "sigue" if len(pool) > limit else ""
        return {"items": [{"name": name} for name in page], "nextPageToken": token}

    return fetch


def test_list_keeps_the_newest_names_without_paging_the_whole_prefix():
    objects = [f"logs/dt=2026-11-16/{index:04d}.jsonl" for index in range(30)]
    calls: list[str] = []
    names, truncated = _list(
        "bucket",
        "logs/dt=2026-11-16/",
        "tok",
        max_results=5,
        fetch=_fetch_factory(objects, calls),
    )
    assert truncated is True
    assert names == objects[-5:]
    assert len(calls) <= 40
    assert all("pageToken" not in url for url in calls)


def test_list_is_not_truncated_when_the_prefix_ends():
    def fetch(url: str) -> dict:
        assert "maxResults=6" in url
        return {"items": [{"name": "logs/solo.jsonl"}]}

    names, truncated = _list("bucket", "logs/", "tok", max_results=6, fetch=fetch)
    assert truncated is False
    assert names == ["logs/solo.jsonl"]
