from sqlalchemy import create_engine, Column, String, Float, DateTime, Enum, Text, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
from uuid import uuid4
import enum
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://usuario:password@localhost:5432/tigo_payments")

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Business(Base):
    __tablename__ = "businesses"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name = Column(String(100), nullable=False)
    email = Column(String(200), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    api_key = Column(String(64), unique=True, nullable=False, index=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Gmail de verificación (donde llegan los correos del banco)
    gmail_address = Column(String(200), nullable=True)
    gmail_credentials_json = Column(Text, nullable=True)  # contenido de credentials.json
    gmail_token_json = Column(Text, nullable=True)         # contenido de token.json


class PaymentStatus(str, enum.Enum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


class Payment(Base):
    __tablename__ = "payments"

    code = Column(String(20), primary_key=True, index=True)       # Ej: PAY-4521
    business_id = Column(String(100), nullable=False, index=True)  # ID del negocio cliente
    amount = Column(Float, nullable=False)                         # Monto esperado en Bs
    currency = Column(String(10), default="BOB")
    description = Column(String(255), nullable=True)
    status = Column(Enum(PaymentStatus), default=PaymentStatus.PENDING)

    # Datos del pagador (se llenan al confirmar)
    payer_name = Column(String(200), nullable=True)
    payer_phone = Column(String(20), nullable=True)
    paid_amount = Column(Float, nullable=True)
    transaction_id = Column(String(100), nullable=True)
    detection_source = Column(String(10), nullable=True)           # "email" o "sms"

    created_at = Column(DateTime, default=datetime.utcnow)
    confirmed_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=False)

    # Webhook del negocio (para notificar cuando se confirme)
    webhook_url = Column(String(500), nullable=True)


class ProcessedNotification(Base):
    __tablename__ = "processed_notifications"

    notification_id = Column(String(200), primary_key=True)       # ID del email/SMS
    source = Column(String(10))                                    # "email" o "sms"
    raw_content = Column(Text, nullable=True)
    payment_code = Column(String(20), nullable=True)
    processed_at = Column(DateTime, default=datetime.utcnow)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_tables():
    Base.metadata.create_all(bind=engine)


# Exportar Business para usarlo en otros módulos
__all__ = ["Business", "Payment", "ProcessedNotification", "PaymentStatus", "get_db", "create_tables"]
