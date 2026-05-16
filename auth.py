import secrets
from passlib.context import CryptContext
from sqlalchemy.orm import Session
from fastapi import Depends, Header, HTTPException
from database import Business, get_db

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def generate_api_key() -> str:
    return secrets.token_hex(32)


def get_current_business(
    x_api_key: str = Header(..., description="API key de la empresa"),
    db: Session = Depends(get_db),
) -> Business:
    business = db.query(Business).filter(
        Business.api_key == x_api_key,
        Business.is_active == True,
    ).first()
    if not business:
        raise HTTPException(status_code=401, detail="API key inválida o empresa inactiva")
    return business
