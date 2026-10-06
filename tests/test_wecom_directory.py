import httpx
import pytest

from app.config import Settings
from app.wecom.directory import DirectoryAccessError, WeComDirectory


@pytest.mark.asyncio
async def test_directory_authorizes_department_and_descendants() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/gettoken"):
            return httpx.Response(200, json={"errcode": 0, "access_token": "token"})
        if request.url.path.endswith("/department/simplelist"):
            return httpx.Response(
                200,
                json={
                    "errcode": 0,
                    "department_id": [
                        {"id": 1, "parentid": 0},
                        {"id": 4, "parentid": 1},
                        {"id": 19, "parentid": 4},
                        {"id": 32, "parentid": 19},
                        {"id": 6, "parentid": 1},
                    ],
                },
            )
        if request.url.path.endswith("/user/list_id"):
            return httpx.Response(
                200,
                json={
                    "errcode": 0,
                    "dept_user": [
                        {"userid": "sales-root", "department": 4},
                        {"userid": "sales-child", "department": 32},
                        {"userid": "other", "department": 6},
                    ],
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    directory = WeComDirectory(
        Settings(
            _env_file=None,
            wecom_corp_id="ww-company",
            wecom_secret="secret",
            wecom_sales_department_id=4,
            wecom_directory_cache_seconds=300,
        ),
        transport=httpx.MockTransport(handler),
    )

    assert await directory.can_view_product_cost("sales-root") is True
    assert await directory.can_view_product_cost("sales-child") is True
    assert await directory.can_view_product_cost("other") is False
    assert calls.count("/cgi-bin/gettoken") == 1


@pytest.mark.asyncio
async def test_directory_failure_does_not_grant_permission() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errcode": 60020, "errmsg": "not allowed"})

    directory = WeComDirectory(
        Settings(
            _env_file=None,
            wecom_corp_id="ww-company",
            wecom_secret="secret",
            wecom_sales_department_id=4,
        ),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(DirectoryAccessError, match="获取通讯录令牌失败"):
        await directory.can_view_product_cost("sales-root")


@pytest.mark.asyncio
async def test_directory_failure_is_short_cached() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timeout")

    directory = WeComDirectory(
        Settings(
            _env_file=None,
            wecom_corp_id="ww-company",
            wecom_secret="secret",
            wecom_sales_department_id=4,
            wecom_directory_failure_cache_seconds=30,
        ),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(DirectoryAccessError):
        await directory.can_view_product_cost("sales-root")
    with pytest.raises(DirectoryAccessError):
        await directory.can_view_product_cost("sales-root")
    assert calls == 1
