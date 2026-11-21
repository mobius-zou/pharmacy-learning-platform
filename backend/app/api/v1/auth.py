"""认证路由 /api/v1/auth。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...config import settings
from ...database import get_db
from ...deps import api_guard, get_current_user
from ...errors import APIError, ConflictError, UnauthorizedError
from ...logging_config import get_logger
from ...models import User
from ...ratelimit import client_key, get_login_limiter, get_register_limiter
from ...schemas import LoginRequest, TokenResponse, UserCreate, UserOut
from ...security import create_access_token, hash_password, verify_password
from ...timeutils import utcnow

logger = get_logger(__name__)
router = APIRouter(prefix="/auth", tags=["认证"])


@router.post(
    "/register",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    summary="注册用户",
)
@api_guard
def register(
    payload: UserCreate, request: Request, db: Session = Depends(get_db)
) -> User:
    """创建新用户。用户名唯一。

    注册同样受限流保护：否则可以脚本批量刷号（每条都触发一次 bcrypt 哈希，
    本身就是 CPU 放大攻击）。
    """
    limiter = get_register_limiter()
    status_info = limiter.check(client_key(request))
    if status_info.limited:
        raise APIError(
            f"注册过于频繁，请 {status_info.retry_after} 秒后重试",
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            error="too_many_requests",
            detail={"retry_after": status_info.retry_after},
        )
    limiter.record_failure(client_key(request))

    exists = db.execute(
        select(User).where(User.username == payload.username)
    ).scalar_one_or_none()
    if exists is not None:
        raise ConflictError(f"用户名已存在：{payload.username}")

    user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        created_at=utcnow(),
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:  # 并发注册同名用户
        raise ConflictError(f"用户名已存在：{payload.username}") from exc

    logger.info("注册用户 id=%s username=%s", user.id, user.username)
    return user


@router.post("/login", response_model=TokenResponse, summary="登录获取 JWT")
@api_guard
def login(
    payload: LoginRequest, request: Request, db: Session = Depends(get_db)
) -> TokenResponse:
    """用户名 + 密码换取 access token。

    失败次数受滑动窗口限流保护：连续失败达到阈值后返回 429，
    并带 ``Retry-After`` 头，避免密码被无限次爆破。
    """
    limiter = get_login_limiter()
    key = client_key(request, payload.username)

    status_info = limiter.check(key)
    if status_info.limited:
        logger.warning("登录被限流 username=%s retry_after=%s", payload.username, status_info.retry_after)
        raise APIError(
            f"登录失败次数过多，请 {status_info.retry_after} 秒后重试",
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            error="too_many_requests",
            detail={"retry_after": status_info.retry_after},
        )

    user = db.execute(
        select(User).where(User.username == payload.username.strip())
    ).scalar_one_or_none()

    if user is None or not verify_password(payload.password, user.password_hash):
        outcome = limiter.record_failure(key)
        logger.warning(
            "登录失败 username=%s 剩余尝试=%s", payload.username, outcome.remaining
        )
        # 不透露「用户名不存在」还是「密码错误」
        raise UnauthorizedError("用户名或密码错误")

    # 成功登录清零失败计数，避免正常用户被自身操作拖累
    limiter.reset(key)
    token = create_access_token(
        user.id,
        extra_claims={"username": user.username},
        token_version=user.token_version,
    )
    logger.info("登录成功 id=%s username=%s", user.id, user.username)
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        expires_in=settings.jwt_expire_minutes * 60,
        user=UserOut.model_validate(user),
    )


@router.get("/me", response_model=UserOut, summary="当前用户信息")
@api_guard
def read_me(current_user: User = Depends(get_current_user)) -> User:
    """返回当前登录用户。"""
    return current_user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="登出并吊销全部令牌")
@api_guard
def logout(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    """登出：把用户的令牌版本 +1，使其**已签发的所有令牌立即失效**。

    无状态 JWT 无法单独撤销某一个令牌，因此这里采用「版本锚点」方案：
    校验时会比对令牌里的 ``ver`` 与用户当前的 ``token_version``。
    """
    current_user.token_version = int(current_user.token_version or 0) + 1
    db.flush()
    logger.info("用户登出并吊销令牌 id=%s 新版本=%s", current_user.id, current_user.token_version)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
