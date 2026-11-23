"""认证路由 /api/v1/auth。

两种会话承载方式并存：

- **HttpOnly Cookie**（正式前端使用）：token 只存在 Cookie 里，前端 JS 读不到，
  XSS 无法窃取凭据；配合 ``SameSite=Lax`` 挡住绝大部分 CSRF。
- **Bearer 头**（兼容保留）：``POST /login`` 仍返回 ``access_token``，
  ``/ui/literature`` 单页与测试脚本继续用它。

``get_current_user`` 会优先取 Authorization 头，缺失时回退 Cookie，
因此两个客户端可以共存、无需二选一。
"""

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
from ...schemas import LoginRequest, SessionResponse, TokenResponse, UserCreate, UserOut
from ...security import create_access_token, hash_password, verify_password
from ...timeutils import utcnow

logger = get_logger(__name__)
router = APIRouter(prefix="/auth", tags=["认证"])


# --- Cookie 会话 -----------------------------------------------------------


def _set_session_cookie(response: Response, token: str) -> None:
    """把访问令牌写入 HttpOnly Cookie。

    - ``HttpOnly``：JS 读不到，XSS 无法窃取；
    - ``SameSite=Lax``：跨站 POST 不携带 Cookie，挡住绝大部分 CSRF；
    - ``Secure``：生产（HTTPS）必须开启，由 ``AUTH_COOKIE_SECURE`` 控制；
    - ``Max-Age`` 与 token 过期时间一致，避免「Cookie 还在但 token 已失效」。
    """
    response.set_cookie(
        key=settings.cookie_name,
        value=token,
        max_age=settings.jwt_expire_minutes * 60,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,  # type: ignore[arg-type]
        path=settings.cookie_path,
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.cookie_name,
        path=settings.cookie_path,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,  # type: ignore[arg-type]
    )


def _authenticate(
    payload: LoginRequest, request: Request, db: Session
) -> tuple[User, str]:
    """校验用户名密码并签发令牌；失败时走限流与统一错误。"""
    limiter = get_login_limiter()
    key = client_key(request, payload.username)

    status_info = limiter.check(key)
    if status_info.limited:
        logger.warning(
            "登录被限流 username=%s retry_after=%s", payload.username, status_info.retry_after
        )
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
        logger.warning("登录失败 username=%s 剩余尝试=%s", payload.username, outcome.remaining)
        # 不透露「用户名不存在」还是「密码错误」
        raise UnauthorizedError("用户名或密码错误")

    # 成功登录清零失败计数，避免正常用户被自身操作拖累
    limiter.reset(key)
    token = create_access_token(
        user.id,
        extra_claims={"username": user.username},
        token_version=user.token_version,
    )
    return user, token


# --- 注册 -------------------------------------------------------------------


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

    # 显式提交：`get_db` 的 commit 发生在响应发出之后，而「注册成功后立即登录」
    # 是常见动作 —— 若不在这里提交，紧随其后的登录请求可能查不到刚插入的用户
    # （表现为偶发 401「用户名或密码错误」）。提交后再返回，读己之写有保证。
    db.commit()
    logger.info("注册用户 id=%s username=%s", user.id, user.username)
    return user


# --- 登录（两种形态）--------------------------------------------------------


@router.post("/login", response_model=TokenResponse, summary="登录获取 JWT")
@api_guard
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> TokenResponse:
    """用户名 + 密码换取 access token（响应体），同时写入 HttpOnly Cookie。

    响应体里的 ``access_token`` 保留给既有的 ``/ui/literature`` 单页与脚本；
    正式前端请改用 ``POST /auth/session``，它不返回 token。
    """
    user, token = _authenticate(payload, request, db)
    _set_session_cookie(response, token)
    logger.info("登录成功 id=%s username=%s", user.id, user.username)
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        expires_in=settings.jwt_expire_minutes * 60,
        user=UserOut.model_validate(user),
    )


@router.post(
    "/session",
    response_model=SessionResponse,
    summary="登录（HttpOnly Cookie 会话，响应体不含 token）",
)
@api_guard
def login_session(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> SessionResponse:
    """正式前端使用的登录入口。

    与 ``/login`` 的唯一区别：**响应体不返回 access token**。
    令牌只存在于 HttpOnly Cookie 中，前端 JS 无法读取。
    """
    user, token = _authenticate(payload, request, db)
    _set_session_cookie(response, token)
    logger.info("登录成功（Cookie 会话）id=%s username=%s", user.id, user.username)
    return SessionResponse(user=UserOut.model_validate(user))


# --- 当前用户 / 登出 --------------------------------------------------------


@router.get("/me", response_model=UserOut, summary="当前用户信息")
@api_guard
def read_me(current_user: User = Depends(get_current_user)) -> User:
    """返回当前登录用户（支持 Cookie 或 Bearer）。"""
    return current_user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="登出并吊销全部令牌")
@api_guard
def logout(
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    """登出：把用户的令牌版本 +1，并清除会话 Cookie。

    无状态 JWT 无法单独撤销某一个令牌，因此这里采用「版本锚点」方案：
    校验时会比对令牌里的 ``ver`` 与用户当前的 ``token_version``。
    清除 Cookie 只是顺手清理浏览器状态；即使它还在，版本不匹配也会 401。
    """
    current_user.token_version = int(current_user.token_version or 0) + 1
    db.flush()
    _clear_session_cookie(response)
    logger.info("用户登出并吊销令牌 id=%s 新版本=%s", current_user.id, current_user.token_version)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response
