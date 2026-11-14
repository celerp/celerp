"""Every app call that puts an id read from a page or a form into an API path sends it as one
path segment: slashes, query marks and lone dots escaped, so no id reaches another record."""
import inspect

import httpx
import pytest

import ui.api_client as api

WRAPPERS = sorted(name for name, fn in vars(api).items()
                  if inspect.iscoroutinefunction(fn) and fn.__module__ == api.__name__
                  and "_segment(" in inspect.getsource(fn))


def test_every_id_path_wrapper_is_covered():
    assert len(WRAPPERS) >= 20 and {"get_item", "update_fund", "help_image"} <= set(WRAPPERS)


def _args(fn, hostile: str) -> list:
    out = []
    for p in list(inspect.signature(fn).parameters.values())[1:]:
        ann = str(p.annotation)
        out.append({} if "dict" in ann else True if ann in ("bool", "<class 'bool'>") else None if "None" in ann else hostile)
    return out


@pytest.mark.parametrize("hostile, segment", [("..", "%2E%2E"), (".", "%2E"), ("../companies/me", "..%2Fcompanies%2Fme"),
                                              ("a?b#c", "a%3Fb%23c")])
@pytest.mark.parametrize("name", WRAPPERS)
async def test_id_is_sent_as_one_path_segment(name, hostile, segment, monkeypatch):
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    monkeypatch.setattr(api, "_get_transport", lambda: httpx.MockTransport(answer))
    fn = getattr(api, name)
    await fn("token", *_args(fn, hostile))
    assert len(seen) == 1, name
    parts = seen[0].url.raw_path.decode().split("?")[0].split("/")
    assert segment in parts and not {".", ".."} & set(parts), (name, parts)
