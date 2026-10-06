import asyncio
import time
from dataclasses import dataclass
from functools import lru_cache

import httpx

from app.config import Settings, get_settings


class DirectoryAccessError(RuntimeError):
    """企业微信通讯录不可用；调用方必须按无权限处理。"""


@dataclass(frozen=True)
class DirectorySnapshot:
    authorized_user_ids: frozenset[str]
    authorized_department_ids: frozenset[int]
    expires_at: float


class WeComDirectory:
    """只缓存营销中心成员 ID，不在项目中保存姓名、手机等通讯录信息。"""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self._snapshot: DirectorySnapshot | None = None
        self._lock = asyncio.Lock()
        self._failure_until = 0.0
        self._failure_message = "企业微信通讯录暂时不可用"

    @staticmethod
    def _api_error(payload: dict[str, object], operation: str) -> DirectoryAccessError:
        code = payload.get("errcode")
        message = str(payload.get("errmsg") or "企业微信接口返回未知错误").split(" hint:", 1)[0]
        return DirectoryAccessError(f"{operation}失败（{code}）：{message}")

    async def _load_snapshot(self) -> DirectorySnapshot:
        corp_id = self.settings.wecom_corp_id
        secret = self.settings.wecom_secret
        root_department_id = self.settings.wecom_sales_department_id
        if not corp_id or not secret or root_department_id is None:
            raise DirectoryAccessError("企业微信通讯录或营销中心部门 ID 尚未配置")

        try:
            async with httpx.AsyncClient(
                timeout=self.settings.wecom_directory_timeout_seconds,
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                token_response = await client.get(
                    "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                    params={"corpid": corp_id, "corpsecret": secret},
                )
                token_response.raise_for_status()
                token_payload = token_response.json()
                if not isinstance(token_payload, dict):
                    raise ValueError("invalid token response")
                if token_payload.get("errcode") != 0:
                    raise self._api_error(token_payload, "获取通讯录令牌")
                access_token = str(token_payload.get("access_token") or "")
                if not access_token:
                    raise DirectoryAccessError("企业微信没有返回通讯录令牌")

                departments_response = await client.get(
                    "https://qyapi.weixin.qq.com/cgi-bin/department/simplelist",
                    params={"access_token": access_token},
                )
                departments_response.raise_for_status()
                departments_payload = departments_response.json()
                if not isinstance(departments_payload, dict):
                    raise ValueError("invalid departments response")
                if departments_payload.get("errcode") != 0:
                    raise self._api_error(departments_payload, "读取部门层级")
                departments = list(departments_payload.get("department_id") or [])
                known_ids = {
                    int(item["id"])
                    for item in departments
                    if isinstance(item, dict) and item.get("id") is not None
                }
                if root_department_id not in known_ids:
                    raise DirectoryAccessError("配置的营销中心部门 ID 不在企业通讯录中")
                authorized_departments = {root_department_id}
                changed = True
                while changed:
                    changed = False
                    for item in departments:
                        if not isinstance(item, dict):
                            continue
                        department_id = int(item.get("id") or 0)
                        parent_id = int(item.get("parentid") or 0)
                        if (
                            parent_id in authorized_departments
                            and department_id not in authorized_departments
                        ):
                            authorized_departments.add(department_id)
                            changed = True

                authorized_users: set[str] = set()
                cursor = ""
                while True:
                    members_response = await client.post(
                        "https://qyapi.weixin.qq.com/cgi-bin/user/list_id",
                        params={"access_token": access_token},
                        json={"cursor": cursor, "limit": 10000},
                    )
                    members_response.raise_for_status()
                    members_payload = members_response.json()
                    if not isinstance(members_payload, dict):
                        raise ValueError("invalid members response")
                    if members_payload.get("errcode") != 0:
                        raise self._api_error(members_payload, "读取成员部门归属")
                    for member in list(members_payload.get("dept_user") or []):
                        if not isinstance(member, dict):
                            continue
                        departments_value = member.get("department")
                        member_departments = (
                            departments_value
                            if isinstance(departments_value, list)
                            else [departments_value]
                            if departments_value is not None
                            else []
                        )
                        if any(int(item) in authorized_departments for item in member_departments):
                            user_id = str(member.get("userid") or "").strip()
                            if user_id:
                                authorized_users.add(user_id)
                    next_cursor = str(members_payload.get("next_cursor") or "")
                    if not next_cursor or next_cursor == cursor:
                        break
                    cursor = next_cursor
        except DirectoryAccessError:
            raise
        except (
            httpx.HTTPError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            IndexError,
        ) as exc:
            raise DirectoryAccessError("企业微信通讯录暂时不可用") from exc

        return DirectorySnapshot(
            authorized_user_ids=frozenset(authorized_users),
            authorized_department_ids=frozenset(authorized_departments),
            expires_at=time.monotonic() + self.settings.wecom_directory_cache_seconds,
        )

    async def snapshot(self) -> DirectorySnapshot:
        current = self._snapshot
        if current is not None and current.expires_at > time.monotonic():
            return current
        if self._failure_until > time.monotonic():
            raise DirectoryAccessError(self._failure_message)
        async with self._lock:
            current = self._snapshot
            if current is not None and current.expires_at > time.monotonic():
                return current
            if self._failure_until > time.monotonic():
                raise DirectoryAccessError(self._failure_message)
            try:
                current = await self._load_snapshot()
            except DirectoryAccessError as exc:
                self._failure_message = str(exc) or "企业微信通讯录暂时不可用"
                self._failure_until = (
                    time.monotonic()
                    + self.settings.wecom_directory_failure_cache_seconds
                )
                raise
            self._failure_until = 0.0
            self._failure_message = "企业微信通讯录暂时不可用"
            self._snapshot = current
            return current

    async def can_view_product_cost(self, user_id: str) -> bool:
        if not user_id:
            return False
        return user_id in (await self.snapshot()).authorized_user_ids


@lru_cache
def get_wecom_directory() -> WeComDirectory:
    return WeComDirectory(get_settings())
