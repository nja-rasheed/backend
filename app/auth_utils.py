from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.user import User

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Use HTTPBearer instead of OAuth2PasswordBearer
security = HTTPBearer()


def verify_password(plain_password: str, hashed_password: str) -> bool:
  return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
  return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
  to_encode = data.copy()
  expire = datetime.now(timezone.utc) + (
      expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
  )
  to_encode.update({"exp": expire})
  return jwt.encode(
      to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM
  )


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
) -> User:
  credentials_exception = HTTPException(
      status_code=status.HTTP_401_UNAUTHORIZED,
      detail="Could not validate credentials",
      headers={"WWW-Authenticate": "Bearer"},
  )
  token = credentials.credentials
  try:
    payload = jwt.decode(
        token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
    )
    user_id: str = payload.get("sub")
    if user_id is None:
      raise credentials_exception
  except JWTError:
    raise credentials_exception

  user = db.query(User).filter(User.id == int(user_id)).first()
  if user is None:
    raise credentials_exception
  return user