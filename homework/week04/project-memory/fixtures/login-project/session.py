"""可独立运行的最小登录会话到期规则。"""

SESSION_TTL_SECONDS = 30 * 60


def create_session(user_id: str, now: int) -> dict:
    if not user_id:
        raise ValueError("user_id 不能为空")
    return {"user_id": user_id, "expires_at": now + SESSION_TTL_SECONDS}


def is_session_valid(session: dict, now: int) -> bool:
    return now < session["expires_at"]
