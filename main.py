from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import random
import string
import os
from dotenv import load_dotenv

from database import get_db, create_tables, Payment, PaymentStatus, ProcessedNotification, Business
from auth import hash_password, verify_password, generate_api_key, get_current_business
from payment_checker import (
    match_payment, confirm_payment, register_notification,
    is_already_processed, expire_old_payments, send_webhook
)
from email_reader import fetch_new_tigo_emails

load_dotenv()

app = FastAPI(
    title="Tigo Money Payment Verifier API",
    description="API para verificar pagos de Tigo Money via Email y SMS parsing",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

PAYMENT_EXPIRY_MINUTES = int(os.getenv("PAYMENT_EXPIRY_MINUTES", 30))


# ─── Schemas ────────────────────────────────────────────────────────────────

class RegisterBusinessRequest(BaseModel):
    name: str
    email: EmailStr
    password: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class BusinessResponse(BaseModel):
    id: str
    name: str
    email: str
    api_key: str
    created_at: datetime


class GmailConfigRequest(BaseModel):
    gmail_address: str
    credentials_json: str
    token_json: str


class CreatePaymentRequest(BaseModel):
    amount: float
    description: str | None = None
    webhook_url: str | None = None
    expiry_minutes: int = PAYMENT_EXPIRY_MINUTES


class ManualVerifyRequest(BaseModel):
    amount: float
    payer_name: str | None = None
    payer_phone: str | None = None
    transaction_id: str | None = None


# ─── Helpers ─────────────────────────────────────────────────────────────────

def generate_payment_code() -> str:
    digits = "".join(random.choices(string.digits, k=6))
    return f"PAY-{digits}"


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
            "message": f"El pagador debe escribir '{payment.code}' en el concepto/descripción del pago",
            "amount": f"Bs. {payment.amount:.2f}",
            "reference_code": payment.code,
            "expires_at": payment.expires_at.isoformat(),
        }

    return data


# ─── Empresas: registro y login ───────────────────────────────────────────────

@app.post("/businesses/register", summary="Registrar una nueva empresa", tags=["Empresas"])
def register_business(body: RegisterBusinessRequest, db: Session = Depends(get_db)):
    """
    Registra una nueva empresa. Devuelve su API key única que debe usar en
    el header `x-api-key` de todas las demás requests.
    """
    existing = db.query(Business).filter(Business.email == body.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Ya existe una empresa con ese email")

    business = Business(
        name=body.name,
        email=body.email,
        hashed_password=hash_password(body.password),
        api_key=generate_api_key(),
    )
    db.add(business)
    db.commit()
    db.refresh(business)

    return {
        "id": business.id,
        "name": business.name,
        "email": business.email,
        "api_key": business.api_key,
        "created_at": business.created_at,
        "message": "Empresa registrada. Guarda tu api_key — la necesitarás en cada request.",
    }


@app.post("/businesses/login", summary="Login de empresa", tags=["Empresas"])
def login_business(body: LoginRequest, db: Session = Depends(get_db)):
    """
    Autentica una empresa con email y contraseña. Devuelve la API key.
    """
    business = db.query(Business).filter(Business.email == body.email).first()
    if not business or not verify_password(body.password, business.hashed_password):
        raise HTTPException(status_code=401, detail="Email o contraseña incorrectos")

    if not business.is_active:
        raise HTTPException(status_code=403, detail="Empresa desactivada")

    return {
        "id": business.id,
        "name": business.name,
        "email": business.email,
        "api_key": business.api_key,
    }


@app.get("/businesses/me", summary="Info de la empresa autenticada", tags=["Empresas"])
def get_me(business: Business = Depends(get_current_business)):
    """Retorna los datos de la empresa autenticada por su API key."""
    return {
        "id": business.id,
        "name": business.name,
        "email": business.email,
        "created_at": business.created_at,
        "gmail_configured": business.gmail_address is not None,
        "gmail_address": business.gmail_address,
    }


@app.put("/businesses/me/gmail", summary="Configurar Gmail de verificación", tags=["Empresas"])
def configure_gmail(
    body: GmailConfigRequest,
    db: Session = Depends(get_db),
    business: Business = Depends(get_current_business),
):
    """
    Configura el Gmail donde llegan los correos del banco para esta empresa.

    Pasos para obtener credentials_json y token_json:
    1. Descarga credentials.json desde Google Cloud Console (OAuth 2.0 Client ID)
    2. Ejecuta localmente: python generate_token.py (o el flujo OAuth una vez)
    3. Copia el contenido de credentials.json y token.json como strings en este endpoint
    """
    import json as _json
    try:
        _json.loads(body.credentials_json)
        _json.loads(body.token_json)
    except ValueError:
        raise HTTPException(status_code=400, detail="credentials_json o token_json no son JSON válido")

    business.gmail_address = body.gmail_address
    business.gmail_credentials_json = body.credentials_json
    business.gmail_token_json = body.token_json
    db.commit()

    return {
        "message": f"Gmail '{body.gmail_address}' configurado correctamente para {business.name}",
        "gmail_address": business.gmail_address,
    }


@app.delete("/businesses/me/gmail", summary="Quitar configuración de Gmail", tags=["Empresas"])
def remove_gmail(
    db: Session = Depends(get_db),
    business: Business = Depends(get_current_business),
):
    business.gmail_address = None
    business.gmail_credentials_json = None
    business.gmail_token_json = None
    db.commit()
    return {"message": "Configuración de Gmail eliminada"}


# ─── Endpoints de pagos (scoped por empresa) ─────────────────────────────────

@app.post("/payments", summary="Crear una orden de pago", tags=["Pagos"])
def create_payment(
    body: CreatePaymentRequest,
    db: Session = Depends(get_db),
    business: Business = Depends(get_current_business),
):
    """
    Crea una orden de pago bajo la empresa autenticada.
    El pagador debe incluir el código generado en el concepto de su transferencia.
    """
    if body.amount <= 0:
        raise HTTPException(status_code=400, detail="El monto debe ser mayor a 0")

    code = generate_payment_code()
    while db.query(Payment).filter(Payment.code == code).first():
        code = generate_payment_code()

    payment = Payment(
        code=code,
        business_id=business.id,
        amount=body.amount,
        description=body.description,
        webhook_url=body.webhook_url,
        expires_at=datetime.utcnow() + timedelta(minutes=body.expiry_minutes),
    )

    db.add(payment)
    db.commit()
    db.refresh(payment)

    return payment_to_response(payment, include_instructions=True)


@app.get("/payments/{code}", summary="Consultar estado de un pago", tags=["Pagos"])
def get_payment(
    code: str,
    db: Session = Depends(get_db),
    business: Business = Depends(get_current_business),
):
    """Retorna el estado de una orden de pago. Solo accesible por la empresa que la creó."""
    payment = db.query(Payment).filter(
        Payment.code == code,
        Payment.business_id == business.id,
    ).first()
    if not payment:
        raise HTTPException(status_code=404, detail=f"Pago '{code}' no encontrado")

    return payment_to_response(payment)


@app.get("/payments", summary="Listar pagos de la empresa", tags=["Pagos"])
def list_payments(
    status: str | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
    business: Business = Depends(get_current_business),
):
    """Lista todas las órdenes de pago de la empresa autenticada."""
    query = db.query(Payment).filter(Payment.business_id == business.id)

    if status:
        try:
            status_enum = PaymentStatus(status.upper())
            query = query.filter(Payment.status == status_enum)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Estado inválido: {status}. Usa: PENDING, CONFIRMED, EXPIRED, FAILED"
            )

    payments = query.order_by(Payment.created_at.desc()).limit(limit).all()
    return [payment_to_response(p) for p in payments]


@app.post("/payments/{code}/verify-manual", summary="Verificar pago manualmente", tags=["Pagos"])
def verify_manual(
    code: str,
    body: ManualVerifyRequest,
    db: Session = Depends(get_db),
    business: Business = Depends(get_current_business),
):
    """
    Confirma un pago manualmente cuando el parser no lo detectó automáticamente.
    Solo la empresa que creó el pago puede confirmarlo.
    """
    payment = db.query(Payment).filter(
        Payment.code == code,
        Payment.business_id == business.id,
    ).first()
    if not payment:
        raise HTTPException(status_code=404, detail=f"Pago '{code}' no encontrado")

    if payment.status == PaymentStatus.CONFIRMED:
        raise HTTPException(status_code=400, detail="Este pago ya fue confirmado")

    if payment.status == PaymentStatus.EXPIRED:
        raise HTTPException(status_code=400, detail="Este pago ya expiró")

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


@app.get("/stats", summary="Estadísticas de la empresa", tags=["Pagos"])
def get_stats(
    db: Session = Depends(get_db),
    business: Business = Depends(get_current_business),
):
    """Retorna estadísticas de pagos de la empresa autenticada."""
    query = db.query(Payment).filter(Payment.business_id == business.id)

    total = query.count()
    confirmed = query.filter(Payment.status == PaymentStatus.CONFIRMED).count()
    pending = query.filter(Payment.status == PaymentStatus.PENDING).count()
    expired = query.filter(Payment.status == PaymentStatus.EXPIRED).count()

    confirmed_payments = db.query(Payment).filter(
        Payment.business_id == business.id,
        Payment.status == PaymentStatus.CONFIRMED,
    ).all()
    total_amount = sum(p.paid_amount or 0 for p in confirmed_payments)

    return {
        "business": business.name,
        "total_orders": total,
        "confirmed": confirmed,
        "pending": pending,
        "expired": expired,
        "total_amount_confirmed_bs": round(total_amount, 2),
        "conversion_rate": f"{(confirmed / total * 100):.1f}%" if total > 0 else "0%",
    }


# ─── Sistema ──────────────────────────────────────────────────────────────────

@app.post("/check-now", summary="Forzar revisión de emails y SMS", tags=["Sistema"])
async def check_now(background_tasks: BackgroundTasks):
    """Fuerza una revisión inmediata de emails y SMS de Tigo Money."""
    background_tasks.add_task(run_payment_check)
    return {"message": "Revisión iniciada en background", "timestamp": datetime.utcnow().isoformat()}


@app.get("/health", tags=["Sistema"])
def health(db: Session = Depends(get_db)):
    try:
        db.execute("SELECT 1")
        db_status = "ok"
    except Exception:
        db_status = "error"

    return {"status": "ok", "database": db_status, "timestamp": datetime.utcnow().isoformat()}


@app.get("/", tags=["Sistema"])
def root():
    return {
        "service": "Tigo Money Payment Verifier API",
        "version": "2.0.0",
        "auth": "Todas las rutas de pagos requieren header x-api-key",
        "endpoints": {
            "POST /businesses/register": "Registrar empresa",
            "POST /businesses/login": "Login (obtener api_key)",
            "GET  /businesses/me": "Info de tu empresa",
            "PUT  /businesses/me/gmail": "Configurar Gmail de verificación",
            "DELETE /businesses/me/gmail": "Quitar Gmail de verificación",
            "POST /payments": "Crear orden de pago",
            "GET  /payments": "Listar tus pagos",
            "GET  /payments/{code}": "Consultar estado de pago",
            "POST /payments/{code}/verify-manual": "Confirmar pago manualmente",
            "GET  /stats": "Tus estadísticas",
        }
    }


# ─── Background Job ───────────────────────────────────────────────────────────

async def run_payment_check():
    from database import SessionLocal
    db = SessionLocal()

    try:
        expired_count = expire_old_payments(db)
        if expired_count:
            print(f"[JOB] {expired_count} pagos expirados")

        # Revisar Gmail de cada empresa que tenga uno configurado
        businesses_with_gmail = db.query(Business).filter(
            Business.gmail_token_json != None,
            Business.is_active == True,
        ).all()

        for business in businesses_with_gmail:
            await _check_business_emails(db, business)

    except Exception as e:
        print(f"[JOB] Error en revisión: {e}")
    finally:
        db.close()


async def _check_business_emails(db, business: Business):
    try:
        email_payments = fetch_new_tigo_emails(
            credentials_json=business.gmail_credentials_json,
            token_json=business.gmail_token_json,
        )
        for parsed in email_payments:
            email_id = parsed.get("email_id")
            # Prefijamos el email_id con el business.id para evitar colisiones entre empresas
            unique_id = f"{business.id}:{email_id}"
            if not email_id or is_already_processed(db, unique_id):
                continue

            payment = match_payment(db, parsed, source="email", business_id=business.id)
            if payment:
                confirmed = confirm_payment(db, payment, parsed, source="email")
                register_notification(db, unique_id, "email", str(parsed), confirmed.code)
                await send_webhook(confirmed)
                print(f"[{business.name}] ✅ Pago confirmado: {confirmed.code} — Bs. {confirmed.paid_amount}")
            else:
                register_notification(db, unique_id, "email", str(parsed))
                print(f"[{business.name}] ⚠️ Email sin orden coincidente: Bs. {parsed.get('amount')}")
    except Exception as e:
        print(f"[{business.name}] Error revisando Gmail: {e}")


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
