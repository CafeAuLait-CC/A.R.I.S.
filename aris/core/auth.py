from sqlalchemy.orm import Session
from ..modules.gpu.models import User


def get_or_create_user(
    db: Session,
    slack_user_id: str,
    display_name: str | None = None,
) -> User:
    """
    Slack -> Internal User Mapping：
    - 以 slack_uid 为准；
    - 第一次出现就建用户；
    - 之后更新 display_name。
    """
    user = db.query(User).filter(User.slack_uid == slack_user_id).one_or_none()
    if not user:
        user = User(
            slack_uid=slack_user_id,
            slack_display_name=display_name or slack_user_id,
        )
        db.add(user)
    else:
        if display_name:
            user.slack_display_name = display_name

    db.commit()
    db.refresh(user)
    return user
