"""Auth module: bcrypt password hashing + JWT bearer tokens for FastAPI.

Used by server.py for /api/auth/* endpoints and protect write actions.

Two roles:
- admin: full access
- viewer: read-only, scoped to assigned_persona_id
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, Literal

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field, EmailStr

JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 14  # long-ish for mobile convenience


def get_jwt_secret() -> str:
    secret = os.environ.get("JWT_SECRET")
    if not secret:
        raise RuntimeError("JWT_SECRET not set in .env")
    return secret


def hash_password(password: str) -> str:
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def create_access_token(user_id: str, email: str, role: str) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS),
        "type": "access",
    }
    return jwt.encode(payload, get_jwt_secret(), algorithm=JWT_ALGORITHM)


# --------- Pydantic models ----------
Role = Literal["admin", "viewer"]


class UserCreate(BaseModel):
    email: EmailStr
    password: str
    nome: Optional[str] = ""
    role: Role = "viewer"
    assigned_persona_id: Optional[str] = ""


class UserPublic(BaseModel):
    id: str
    email: str
    nome: str = ""
    role: Role
    assigned_persona_id: str = ""
    created_at: str


class LoginBody(BaseModel):
    email: EmailStr
    password: str


# --------- Auth dependency ----------
bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    request: Request,
    creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
) -> dict:
    """Validate Bearer token, return user dict (without password_hash)."""
    token = creds.credentials if creds else None
    if not token:
        # fallback: also accept ?token= query (handy for share links / debug)
        token = request.query_params.get("token")
    if not token:
        raise HTTPException(status_code=401, detail="Non autenticato")
    try:
        payload = jwt.decode(token, get_jwt_secret(), algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Sessione scaduta")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Token non valido")
    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="Token non valido")
    db = request.app.state.db
    user = await db.users.find_one({"id": payload["sub"]}, {"_id": 0, "password_hash": 0})
    if not user:
        raise HTTPException(status_code=401, detail="Utente non trovato")
    return user


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Solo admin")
    return user


def serialize_user(u: dict) -> dict:
    return {
        "id": u.get("id", ""),
        "email": u.get("email", ""),
        "nome": u.get("nome", ""),
        "role": u.get("role", "viewer"),
        "assigned_persona_id": u.get("assigned_persona_id", ""),
        "created_at": u.get("created_at", ""),
    }


async def seed_admin(db) -> None:
    """Ensure an admin user exists, taking credentials from .env."""
    admin_email = (os.environ.get("ADMIN_EMAIL") or "admin@conteggi.it").strip().lower()
    admin_password = os.environ.get("ADMIN_PASSWORD") or "admin123"
    admin_nome = os.environ.get("ADMIN_NOME") or "Admin"
    existing = await db.users.find_one({"email": admin_email})
    if not existing:
        await db.users.insert_one({
            "id": str(uuid.uuid4()),
            "email": admin_email,
            "password_hash": hash_password(admin_password),
            "nome": admin_nome,
            "role": "admin",
            "assigned_persona_id": "",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    elif not verify_password(admin_password, existing.get("password_hash", "")):
        # Refresh password if env changed
        await db.users.update_one(
            {"email": admin_email},
            {"$set": {"password_hash": hash_password(admin_password), "role": "admin", "nome": admin_nome}},
        )
