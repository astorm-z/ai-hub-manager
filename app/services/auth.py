from __future__ import annotations

import base64
import hashlib
import hmac
import os

from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy.orm import Session

from app.config import settings
from app.models import User


serializer = URLSafeSerializer(settings.session_secret, salt="ai-hub-session")
PBKDF2_ITERATIONS = 310_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ITERATIONS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, iterations, salt_b64, digest_b64 = password_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def has_admin(db: Session) -> bool:
    return db.query(User).count() > 0


def create_admin(db: Session, username: str, password: str) -> User:
    user = User(username=username.strip(), password_hash=hash_password(password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def authenticate(db: Session, username: str, password: str) -> User | None:
    user = db.query(User).filter(User.username == username.strip()).first()
    if not user or not verify_password(password, user.password_hash):
        return None
    return user


def make_session_token(user_id: int) -> str:
    return serializer.dumps({"uid": user_id})


def read_session_token(token: str | None) -> int | None:
    if not token:
        return None
    try:
        data = serializer.loads(token)
    except BadSignature:
        return None
    uid = data.get("uid")
    return int(uid) if uid is not None else None
