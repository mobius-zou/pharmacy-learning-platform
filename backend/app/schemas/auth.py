"""认证相关 Schema。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..config import settings
from .types import UTCDateTime


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64, description="用户名")
    password: str = Field(min_length=6, max_length=128, description="密码（至少 6 位）")

    @field_validator("username")
    @classmethod
    def _strip_username(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("用户名不能为空")
        if any(ch.isspace() for ch in value):
            raise ValueError("用户名不能包含空白字符")
        return value

    @field_validator("password")
    @classmethod
    def _check_password_bytes(cls, value: str) -> str:
        # bcrypt 只处理前 72 字节，超长直接拒绝而不是静默截断
        if len(value.encode("utf-8")) > settings.password_max_bytes:
            raise ValueError(f"密码不能超过 {settings.password_max_bytes} 字节")
        return value


class LoginRequest(BaseModel):
    # 登录入参同样要有上限：否则超长用户名会进 bcrypt/查询路径
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    created_at: UTCDateTime


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="有效期（秒）")
    user: UserOut


class SessionResponse(BaseModel):
    """Cookie 会话登录的响应：只有用户信息，**不含 token**。

    token 只存在于 HttpOnly Cookie 里，前端 JS 读不到 —— 这是把
    「XSS 可窃取凭据」降到「XSS 只能冒用当前会话」的关键。
    """

    user: UserOut
