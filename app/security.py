"""风险快照等敏感只读接口的轻量角色鉴权。

台账系统尚未接入统一认证，这里通过 ``X-User-Role`` 请求头识别调用方角色，
仅授权角色可以生成或下载风险快照；快照内容本身已剔除联系人敏感字段。
"""

from typing import Optional

from fastapi import Header, HTTPException

from .errors import HTTPStatus, ERROR_FORBIDDEN, fmt

ROLE_ADMIN = "admin"
ROLE_INVESTMENT_LEAD = "investment_lead"
ROLE_VIEWER = "viewer"

ROLE_PERMISSIONS = {
    ROLE_ADMIN: {"snapshot:generate", "snapshot:download"},
    ROLE_INVESTMENT_LEAD: {"snapshot:generate", "snapshot:download"},
    ROLE_VIEWER: set(),
}


def require_permission(permission: str):
    """生成一个 FastAPI 依赖，校验调用方角色是否具备指定权限。"""

    def checker(x_user_role: Optional[str] = Header(None)) -> str:
        if not x_user_role:
            raise HTTPException(
                status_code=HTTPStatus.FORBIDDEN,
                detail=ERROR_FORBIDDEN["role_missing"],
            )
        permissions = ROLE_PERMISSIONS.get(x_user_role)
        if permissions is None:
            raise HTTPException(
                status_code=HTTPStatus.FORBIDDEN,
                detail=fmt(ERROR_FORBIDDEN["role_unknown"], role=x_user_role),
            )
        if permission not in permissions:
            raise HTTPException(
                status_code=HTTPStatus.FORBIDDEN,
                detail=ERROR_FORBIDDEN[permission],
            )
        return x_user_role

    return checker
