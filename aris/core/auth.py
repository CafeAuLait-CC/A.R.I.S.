from sqlalchemy.orm import Session
from . import db as db_module
from ..modules.gpu.models import User  # temporary use the same model

def get_or_create_user(db: Session, slack_user_id: str, display_name: str | None = None) -> User:
    user = db.query(User).filter(User.slack_id == slack_user_id).one_or_none()
    if not user:
        user = User(slack_id=slack_user_id, display_name=display_name or slack_user_id)
        db.add(user)
        db.commit()
        db.refresh(user)
    return user
