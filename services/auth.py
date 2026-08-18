"""App login, session helpers, and permission checks."""

from __future__ import annotations

import logging
import os
from functools import wraps
from typing import Any, Callable

from flask import flash, g, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from services.database import User, get_session

logger = logging.getLogger(__name__)

PERMISSION_KEYS = (
    "perm_home",
    "perm_office",
    "perm_parlours",
    "perm_events",
    "perm_stock",
    "perm_sync_outlook",
    "perm_sync_onedrive",
    "perm_sync_dataflow",
)

PERMISSION_LABELS = {
    "perm_home": "Home",
    "perm_office": "Office",
    "perm_parlours": "Parlours",
    "perm_events": "Events",
    "perm_stock": "Stock Inventory",
    "perm_sync_outlook": "Sync Outlook",
    "perm_sync_onedrive": "Sync OneDrive",
    "perm_sync_dataflow": "Sync DataFlow",
}

PUBLIC_ENDPOINTS = {
    "login",
    "logout",
    "health",
    "static",
}


def hash_password(password: str) -> str:
    return generate_password_hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    return check_password_hash(password_hash, password)


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def get_user_by_id(user_id: int) -> User | None:
    with get_session() as db:
        user = db.query(User).filter_by(id=user_id).first()
        if user is not None:
            db.expunge(user)
        return user


def get_user_by_email(email: str) -> User | None:
    with get_session() as db:
        user = db.query(User).filter_by(email=_normalize_email(email)).first()
        if user is not None:
            db.expunge(user)
        return user


def list_users() -> list[User]:
    with get_session() as db:
        users = db.query(User).order_by(User.is_admin.desc(), User.email.asc()).all()
        db.expunge_all()
        return users


def count_active_admins() -> int:
    with get_session() as db:
        return (
            db.query(User)
            .filter_by(is_admin=True, is_active=True)
            .count()
        )


def authenticate(email: str, password: str) -> User | None:
    user = get_user_by_email(email)
    if user is None or not user.is_active:
        return None
    if not verify_password(user.password_hash, password):
        return None
    return user


def permissions_from_form(form) -> dict[str, bool]:
    return {key: bool(form.get(key)) for key in PERMISSION_KEYS}


def create_user(
    *,
    email: str,
    password: str,
    is_admin: bool = False,
    is_active: bool = True,
    permissions: dict[str, bool] | None = None,
) -> User:
    email_norm = _normalize_email(email)
    if not email_norm or "@" not in email_norm:
        raise ValueError("Enter a valid email address.")
    if not password or len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    if get_user_by_email(email_norm):
        raise ValueError("A user with that email already exists.")

    perms = {key: False for key in PERMISSION_KEYS}
    if permissions:
        perms.update({key: bool(permissions.get(key)) for key in PERMISSION_KEYS})
    if is_admin:
        perms = {key: True for key in PERMISSION_KEYS}

    with get_session() as db:
        user = User(
            email=email_norm,
            password_hash=hash_password(password),
            is_admin=bool(is_admin),
            is_active=bool(is_active),
            **perms,
        )
        db.add(user)
        db.flush()
        db.expunge(user)
        return user


def update_user(
    user_id: int,
    *,
    email: str | None = None,
    is_admin: bool | None = None,
    is_active: bool | None = None,
    permissions: dict[str, bool] | None = None,
    password: str | None = None,
) -> User:
    with get_session() as db:
        user = db.query(User).filter_by(id=user_id).first()
        if user is None:
            raise ValueError("User not found.")

        becoming_non_admin = (
            is_admin is False and user.is_admin and user.is_active
        )
        becoming_inactive_admin = (
            is_active is False and user.is_admin and user.is_active
        )
        if becoming_non_admin or becoming_inactive_admin:
            admin_count = (
                db.query(User).filter_by(is_admin=True, is_active=True).count()
            )
            if admin_count <= 1:
                raise ValueError("Cannot remove or deactivate the last admin.")

        if email is not None:
            email_norm = _normalize_email(email)
            if not email_norm or "@" not in email_norm:
                raise ValueError("Enter a valid email address.")
            clash = (
                db.query(User)
                .filter(User.email == email_norm, User.id != user_id)
                .first()
            )
            if clash:
                raise ValueError("A user with that email already exists.")
            user.email = email_norm

        if is_admin is not None:
            user.is_admin = bool(is_admin)
        if is_active is not None:
            user.is_active = bool(is_active)
        if permissions is not None:
            for key in PERMISSION_KEYS:
                setattr(user, key, bool(permissions.get(key)))
        if user.is_admin:
            for key in PERMISSION_KEYS:
                setattr(user, key, True)
        if password:
            if len(password) < 8:
                raise ValueError("Password must be at least 8 characters.")
            user.password_hash = hash_password(password)

        db.flush()
        db.expunge(user)
        return user


def delete_user(user_id: int, *, acting_user_id: int | None = None) -> None:
    with get_session() as db:
        user = db.query(User).filter_by(id=user_id).first()
        if user is None:
            raise ValueError("User not found.")
        if acting_user_id is not None and user.id == acting_user_id:
            raise ValueError("You cannot delete your own account while signed in.")
        if user.is_admin and user.is_active:
            admin_count = (
                db.query(User).filter_by(is_admin=True, is_active=True).count()
            )
            if admin_count <= 1:
                raise ValueError("Cannot delete the last admin.")
        db.delete(user)


def seed_admin_user() -> tuple[User | None, str | None]:
    """
    Create the bootstrap admin from ADMIN_EMAIL / ADMIN_PASSWORD if no users exist.
    Returns (user, error_message). Does not overwrite an existing admin password.
    """
    with get_session() as db:
        existing = db.query(User).count()
        if existing:
            return None, None

    email = os.environ.get("ADMIN_EMAIL", "").strip()
    password = os.environ.get("ADMIN_PASSWORD", "").strip()
    if not email or not password:
        msg = (
            "No users yet. Set ADMIN_EMAIL and ADMIN_PASSWORD in the environment "
            "to create the first admin account."
        )
        logger.error(msg)
        return None, msg

    try:
        user = create_user(
            email=email,
            password=password,
            is_admin=True,
            is_active=True,
            permissions={key: True for key in PERMISSION_KEYS},
        )
        logger.info("Seeded admin user %s", user.email)
        return user, None
    except Exception as exc:
        logger.exception("Failed to seed admin user")
        return None, str(exc)


def login_user(user: User) -> None:
    session.clear()
    session["user_id"] = user.id
    session["user_email"] = user.email
    session.permanent = True


def logout_user() -> None:
    session.clear()


def current_user() -> User | None:
    if getattr(g, "_current_user", None) is not None:
        return g._current_user
    user_id = session.get("user_id")
    if not user_id:
        g._current_user = None
        return None
    user = get_user_by_id(int(user_id))
    if user is None or not user.is_active:
        logout_user()
        g._current_user = None
        return None
    g._current_user = user
    return user


def user_has_permission(user: User | None, permission: str) -> bool:
    if user is None or not user.is_active:
        return False
    if user.is_admin:
        return True
    if permission not in PERMISSION_KEYS:
        return False
    return bool(getattr(user, permission, False))


def first_allowed_endpoint(user: User) -> str:
    mapping = (
        ("perm_home", "home"),
        ("perm_office", "office"),
        ("perm_parlours", "parlours"),
        ("perm_events", "events"),
        ("perm_stock", "stock_inventory"),
    )
    for perm, endpoint in mapping:
        if user_has_permission(user, perm):
            return endpoint
    if user.is_admin:
        return "users_admin"
    return "login"


def login_required(view: Callable) -> Callable:
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if user is None:
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def permission_required(permission: str) -> Callable:
    def decorator(view: Callable) -> Callable:
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if user is None:
                return redirect(url_for("login", next=request.path))
            if not user_has_permission(user, permission):
                flash("You do not have permission to view that page.", "error")
                return redirect(url_for(first_allowed_endpoint(user)))
            return view(*args, **kwargs)

        return wrapped

    return decorator


def admin_required(view: Callable) -> Callable:
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if user is None:
            return redirect(url_for("login", next=request.path))
        if not user.is_admin:
            flash("Admin access required.", "error")
            return redirect(url_for(first_allowed_endpoint(user)))
        return view(*args, **kwargs)

    return wrapped


def user_to_template(user: User | None) -> dict[str, Any] | None:
    if user is None:
        return None
    data = {
        "id": user.id,
        "email": user.email,
        "is_admin": user.is_admin,
        "is_active": user.is_active,
    }
    for key in PERMISSION_KEYS:
        data[key] = user_has_permission(user, key)
    return data
