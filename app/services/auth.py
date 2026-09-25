"""风险快照接口的 Bearer Token 鉴权。

令牌通过配置项 ``RISK_SNAPSHOT_TOKENS`` 下发，格式为
``姓名:角色:令牌``，多条以英文逗号分隔。角色权限：

- director 局长/负责人：可生成、查看、下载、查询审计记录
- analyst  分析师：可生成、查看、下载
- viewer   专员：仅可查看、下载

未携带令牌或令牌无效返回 401；令牌有效但角色不足返回 403。
"""

from dataclasses import dataclass
from typing import Dict, List

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..config import settings
from ..errors import HTTPStatus

# 角色 → 权限等级，数值越大权限越高
ROLE_LEVEL: Dict[str, int] = {
    "viewer": 1,
    "analyst": 2,
    "director": 3,
}

GENERATE_ROLES = {"director", "analyst"}

_bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class SnapshotUser:
    name: str
    role: str
    token: str


def _load_token_map() -> Dict[str, SnapshotUser]:
    users: Dict[str, SnapshotUser] = {}
    for entry in (settings.RISK_SNAPSHOT_TOKENS or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) != 3:
            continue
        name, role, token = (p.strip() for p in parts)
        if role not in ROLE_LEVEL or not token:
            continue
        users[token] = SnapshotUser(name=name, role=role, token=token)
    return users


def authenticate(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
) -> SnapshotUser:
    """解析并校验 Bearer 令牌，失败时返回 401。"""
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=HTTPStatus.UNAUTHORIZED,
            detail="缺少访问令牌，请在 Authorization 头中携带 Bearer Token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = _load_token_map().get(credentials.credentials)
    if user is None:
        raise HTTPException(
            status_code=HTTPStatus.UNAUTHORIZED,
            detail="访问令牌无效或已失效",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def require_generate_user(
    user: SnapshotUser = Depends(authenticate),
) -> SnapshotUser:
    """生成快照需要 analyst 及以上角色，否则 403。"""
    if user.role not in GENERATE_ROLES:
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail="权限不足：仅负责人或分析师可生成风险快照",
        )
    return user


def require_read_user(
    user: SnapshotUser = Depends(authenticate),
) -> SnapshotUser:
    """查看/下载/审计仅需有效令牌（viewer 及以上）。"""
    return user
