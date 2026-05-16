from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import random
import string
import os
from dotenv import load_dotenv

from database import get_db, create_tables, Payment, PaymentStatus, ProcessedNotification
from payment_checker import (
    match_payment, confirm_payment, register_notification,
    is_already_processed, expire_old_payments, send_webhook
)
from email_reader import fetch_new_tigo_emails

load_dotenv()

app = FastAPI(
    title="Tigo Money Payment Verifier API",
    description="API para verificar pagos de Tigo Money via Email y SMS parsing",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

API_SECRET_KEY = os.getenv("API_SECRET_KEY", "secret")
PAYMENT_EXPIRY_MINUTES = int(os.getenv("PAYMENT_EXPIRY_MINUTES", 30))


# ─── Schemas ────────────────────────────────────────────────────────────────

class CreatePaymentRequest(BaseModel):
    business_id: str
    amount: float
    description: str | None = None
    webhook_url: str | None = None
    expiry_minutes: int = PAYMENT_EXPIRY_MINUTES


class PaymentResponse(BaseModel):
    code: str
    business_id: str
    amount: float
    currency: str
    description: str | None
    status: str
    payer_name: str | None
    payer_phone: str | None
    paid_amount: float | None
    transaction_id: str | None
    detection_source: str | None
    created_at: datetime
    confirmed_at: datetime | None
    expires_at: datetime
    instructions: dict | None = None


class ManualVerifyRequest(BaseModel):
    amount: float
    payer_name: str | None = None
    payer_phone: str | None = None
    transaction_id: str | None = None
    payment_code: str | None = None


# ─── Helpers ─────────────────────────────────────────────────────────────────

def generate_payment_code() -> str:
    digits = "".join(random.choices(string.digits, k=6))
    return f"PAY-{digits}"


def verify_api_key(x_api_key: str = Header(...)):
    if x_api_key != API_SECRET_KEY:
        raise HTTPException(status_code=401, detail="API key inválida")
    return x_api_key


def payment_to_response(payment: Payment, include_instructions: bool = False) -> dict:
    data = {
        "code": payment.code,
        "business_id": payment.business_id,
        "amount": payment.amount,
        "currency": payment.currency,
        "description": payment.description,
        "status": payment.status,
        "payer_name": payment.payer_name,
        "payer_phone": payment.payer_phone,
        "paid_amount": payment.paid_amount,
        "transaction_id": payment.transaction_id,
        "detection_source": payment.detection_source,
        "created_at": payment.created_at,
        "confirmed_at": payment.confirmed_at,
        "expires_at": payment.expires_at,
    }

    if include_instructions:
        data["instructions"] = {
            "message": f"El pagador debe escribir '{payment.code}' en el concepto/descripción del pago de Tigo Money",
            "amount": f"Bs. {payment.amount:.2f}",
            "reference_code": payment.code,
            "expires_at": payment.expires_at.isoformat(),
        }

    return data


# ─── Endpoints ───────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {
        "service": "Tigo Money Payment Verifier API",
        "version": "1.0.0",
        "status": "running",
        "endpoints": {
            "POST /payments": "Crear orden de pago",
            "GET /payments/{code}": "Consultar estado de pago",
            "GET /payments": "Listar pagos de un negocio",
            "POST /payments/{code}/verify-manual": "Verificar pago manualmente",
            "POST /check-now": "Forzar revisión de emails y SMS",
            "GET /health": "Estado del sistema",
        }
    }


@app.get("/health")
def health(db: Session = Depends(get_db)):
    try:
        db.execute("SELECT 1")
        db_status = "ok"
    except Exception:
        db_status = "error"

    return {
        "status": "ok",
        "database": db_status,
        "timestamp": datetime.utcnow().isoformat()
    }


@app.post("/payments", summary="Crear una orden de pago")
def create_payment(
    body: CreatePaymentRequest,
    db: Session = Depends(get_db),
    api_key: str = Depends(verify_api_key)
):
    """
    Crea una orden de pago con un código único.
    El pagador debe incluir ese código en el concepto de su transferencia Tigo Money.
    """
    if body.amount <= 0:
        raise HTTPException(status_code=400, detail="El monto debe ser mayor a 0")

    # Generar código único
    code = generate_payment_code()
    while db.query(Payment).filter(Payment.code == code).first():
        code = generate_payment_code()

    payment = Payment(
        code=code,
        business_id=body.business_id,
        amount=body.amount,
        description=body.description,
        webhook_url=body.webhook_url,
        expires_at=datetime.utcnow() + timedelta(minutes=body.expiry_minutes),
    )

    db.add(payment)
    db.commit()
    db.refresh(payment)

    return payment_to_response(payment, include_instructions=True)


@app.get("/payments/{code}", summary="Consultar estado de un pago")
def get_payment(
    code: str,
    db: Session = Depends(get_db),
    api_key: str = Depends(verify_api_key)
):
    """Retorna el estado actual de una orden de pago por su código."""
    payment = db.query(Payment).filter(Payment.code == code).first()
    if not payment:
        raise HTTPException(status_code=404, detail=f"Pago '{code}' no encontrado")

    return payment_to_response(payment)


@app.get("/payments", summary="Listar pagos de un negocio")
def list_payments(
    business_id: str,
    status: str | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
    api_key: str = Depends(verify_api_key)
):
    """Lista todas las órdenes de pago de un negocio, con filtro opcional por estado."""
    query = db.query(Payment).filter(Payment.business_id == business_id)

    if status:
        try:
            status_enum = PaymentStatus(status.upper())
            query = query.filter(Payment.status == status_enum)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Estado inválido: {status}. Usa: PENDING, CONFIRMED, EXPIRED, FAILED")

    payments = query.order_by(Payment.created_at.desc()).limit(limit).all()
    return [payment_to_response(p) for p in payments]


@app.post("/payments/{code}/verify-manual", summary="Verificar pago manualmente")
def verify_manual(
    code: str,
    body: ManualVerifyRequest,
    db: Session = Depends(get_db),
    api_key: str = Depends(verify_api_key)
):
    """
    Permite confirmar un pago manualmente cuando el cliente no escribió el código
    o cuando el parser no lo detectó automáticamente.
    Útil como fallback.
    """
    payment = db.query(Payment).filter(Payment.code == code).first()
    if not payment:
        raise HTTPException(status_code=404, detail=f"Pago '{code}' no encontrado")

    if payment.status == PaymentStatus.CONFIRMED:
        raise HTTPException(status_code=400, detail="Este pago ya fue confirmado")

    if payment.status == PaymentStatus.EXPIRED:
        raise HTTPException(status_code=400, detail="Este pago ya expiró")

    # Validar monto
    if abs(body.amount - payment.amount) > 0.01:
        raise HTTPException(
            status_code=400,
            detail=f"Monto incorrecto. Esperado: Bs. {payment.amount:.2f}, recibido: Bs. {body.amount:.2f}"
        )

    parsed = {
        "amount": body.amount,
        "payer_name": body.payer_name,
        "payer_phone": body.payer_phone,
        "transaction_id": body.transaction_id,
    }

    confirmed = confirm_payment(db, payment, parsed, source="manual")
    return {**payment_to_response(confirmed), "message": "Pago confirmado manualmente"}


@app.post("/check-now", summary="Forzar revisión de emails y SMS")
async def check_now(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    api_key: str = Depends(verify_api_key)
):
    """
    Fuerza una revisión inmediata de emails y SMS de Tigo Money.
    Normalmente esto corre automáticamente cada 10-15 segundos.
    """
    background_tasks.add_task(run_payment_check)
    return {"message": "Revisión iniciada en background", "timestamp": datetime.utcnow().isoformat()}


@app.get("/stats", summary="Estadísticas generales")
def get_stats(
    business_id: str | None = None,
    db: Session = Depends(get_db),
    api_key: str = Depends(verify_api_key)
):
    """Retorna estadísticas de pagos."""
    query = db.query(Payment)
    if business_id:
        query = query.filter(Payment.business_id == business_id)

    total = query.count()
    confirmed = query.filter(Payment.status == PaymentStatus.CONFIRMED).count()
    pending = query.filter(Payment.status == PaymentStatus.PENDING).count()
    expired = query.filter(Payment.status == PaymentStatus.EXPIRED).count()

    confirmed_payments = query.filter(Payment.status == PaymentStatus.CONFIRMED).all()
    total_amount = sum(p.paid_amount or 0 for p in confirmed_payments)

    return {
        "total_orders": total,
        "confirmed": confirmed,
        "pending": pending,
        "expired": expired,
        "total_amount_confirmed_bs": round(total_amount, 2),
        "conversion_rate": f"{(confirmed / total * 100):.1f}%" if total > 0 else "0%",
    }


# ─── Background Job ───────────────────────────────────────────────────────────

async def run_payment_check():
    """
    Job principal que corre automáticamente.
    Lee emails y SMS nuevos de Tigo Money y los cruza con órdenes pendientes.
    """
    from database import SessionLocal
    db = SessionLocal()

    try:
        # 1. Expirar pagos viejos
        expired_count = expire_old_payments(db)
        if expired_count:
            print(f"[JOB] {expired_count} pagos expirados")

        # 2. Leer emails nuevos de Gmail
        email_payments = fetch_new_tigo_emails()
        for parsed in email_payments:
            email_id = parsed.get("email_id")
            if not email_id or is_already_processed(db, email_id):
                continue

            payment = match_payment(db, parsed, source="email")
            if payment:
                confirmed = confirm_payment(db, payment, parsed, source="email")
                register_notification(db, email_id, "email", str(parsed), confirmed.code)
                await send_webhook(confirmed)
                print(f"[EMAIL] ✅ Pago confirmado: {confirmed.code} — Bs. {confirmed.paid_amount}")
            else:
                register_notification(db, email_id, "email", str(parsed))
                print(f"[EMAIL] ⚠️ Email detectado pero sin orden coincidente: Bs. {parsed.get('amount')}")



    except Exception as e:
        print(f"[JOB] Error en revisión: {e}")
    finally:
        db.close()


# ─── Startup ──────────────────────────────────────────────────────────────────

scheduler = AsyncIOScheduler()

@app.on_event("startup")
async def startup():
    create_tables()
    print("✅ Tablas creadas/verificadas")

    email_interval = int(os.getenv("EMAIL_CHECK_INTERVAL_SECONDS", 15))
    scheduler.add_job(run_payment_check, "interval", seconds=email_interval, id="payment_check")
    scheduler.start()
    print(f"✅ Scheduler iniciado — revisando cada {email_interval}s")


@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown()
