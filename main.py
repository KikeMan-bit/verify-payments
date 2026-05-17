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

from database import get_db, create_tables, Payment, PaymentStatus, ProcessedNotification, Business, User, UserRole
from auth import (
    hash_password, verify_password, generate_api_key,
    create_access_token, get_current_user, require_admin, require_user
)
from payment_checker import (
    match_payment, confirm_payment, register_notification,
    is_already_processed, expire_old_payments, send_webhook
)
from email_reader import fetch_new_emails

load_dotenv()

app = FastAPI(
    title="Verify Payments API",
    description="API para verificar pagos via Email y SMS parsing",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

PAYMENT_EXPIRY_MINUTES = int(os.getenv("PAYMENT_EXPIRY_MINUTES", 30))
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "admin-secret-setup")


# ─── Schemas ─────────────────────────────────────────────────────────────────

class AdminSetupRequest(BaseModel):
    first_name: str
    last_name: str
    email: EmailStr
    password: str


class RegisterRequest(BaseModel):
    business_name: str
    first_name: str
    last_name: str
    email: EmailStr
    password: str




class LoginRequest(BaseModel):
    email: EmailStr
    password: str


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


def user_response(user: User, business_name: str = None) -> dict:
    return {
        "id": user.id,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "email": user.email,
        "role": user.role,
        "business_id": user.business_id,
        "business_name": business_name,
        "created_at": user.created_at,
    }


def business_response(business: Business, include_api_key: bool = False) -> dict:
    data = {
        "id": business.id,
        "name": business.name,
        "email": business.email,
        "is_active": business.is_active,
        "gmail_configured": business.gmail_address is not None,
        "gmail_address": business.gmail_address,
        "created_at": business.created_at,
    }
    if include_api_key:
        data["api_key"] = business.api_key
    return data


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
            "message": f"El pagador debe escribir '{payment.code}' en el concepto del pago",
            "amount": f"Bs. {payment.amount:.2f}",
            "reference_code": payment.code,
            "expires_at": payment.expires_at.isoformat(),
        }
    return data


# ─── Auth (público) ───────────────────────────────────────────────────────────

@app.post("/admin/setup", summary="Crear cuenta ADMIN de la plataforma", tags=["Auth"])
def admin_setup(
    body: AdminSetupRequest,
    x_admin_secret: str = Header(...),
    db: Session = Depends(get_db),
):
    """
    Crea la cuenta ADMIN del dueño de la plataforma.
    Requiere el header x-admin-secret definido en el .env.
    Solo funciona si aún no existe ningún ADMIN.
    """
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Secret inválido")

    if db.query(User).filter(User.role == UserRole.ADMIN).first():
        raise HTTPException(status_code=400, detail="Ya existe un administrador de la plataforma")

    if db.query(User).filter(User.email == body.email).first():
        raise HTTPException(status_code=400, detail="El email ya está registrado")

    admin = User(
        first_name=body.first_name,
        last_name=body.last_name,
        email=body.email,
        hashed_password=hash_password(body.password),
        role=UserRole.ADMIN,
        business_id=None,
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)

    token = create_access_token(admin.id, admin.role)
    return {
        "user": user_response(admin),
        "token": token,
    }


@app.post("/auth/register", summary="Registrar empresa (CLIENT)", tags=["Auth"])
def register(body: RegisterRequest, db: Session = Depends(get_db)):
    """
    Una empresa se registra en la plataforma.
    Crea un Business + un usuario CLIENT vinculado a él.
    """
    if db.query(User).filter(User.email == body.email).first():
        raise HTTPException(status_code=400, detail="El email ya está registrado")

    if db.query(Business).filter(Business.email == body.email).first():
        raise HTTPException(status_code=400, detail="Ya existe una empresa con ese email")

    business = Business(
        name=body.business_name,
        email=body.email,
        api_key=generate_api_key(),
    )
    db.add(business)
    db.flush()

    user = User(
        business_id=business.id,
        first_name=body.first_name,
        last_name=body.last_name,
        email=body.email,
        hashed_password=hash_password(body.password),
        role=UserRole.USER,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    db.refresh(business)

    token = create_access_token(user.id, user.role, business.id)
    return {
        "user": user_response(user, business.name),
        "token": token,
    }


@app.post("/auth/login", summary="Login", tags=["Auth"])
def login(body: LoginRequest, db: Session = Depends(get_db)):
    """Login para ADMIN y CLIENT. Devuelve usuario + token JWT."""
    user = db.query(User).filter(User.email == body.email).first()
    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Email o contraseña incorrectos")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Usuario desactivado")

    business_name = None
    if user.business_id:
        business = db.query(Business).filter(Business.id == user.business_id).first()
        business_name = business.name if business else None

    token = create_access_token(user.id, user.role, user.business_id)
    return {
        "user": user_response(user, business_name),
        "token": token,
    }


@app.get("/auth/me", summary="Usuario autenticado", tags=["Auth"])
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    business_name = None
    if user.business_id:
        business = db.query(Business).filter(Business.id == user.business_id).first()
        business_name = business.name if business else None
    return user_response(user, business_name)



# ─── ADMIN: gestión de la plataforma ─────────────────────────────────────────

@app.get("/admin/businesses", summary="Listar todas las empresas", tags=["Admin"])
def admin_list_businesses(
    is_active: bool | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """ADMIN: ve todas las empresas registradas en la plataforma."""
    query = db.query(Business)
    if is_active is not None:
        query = query.filter(Business.is_active == is_active)
    businesses = query.order_by(Business.created_at.desc()).limit(limit).all()
    return [business_response(b) for b in businesses]


@app.get("/admin/businesses/{business_id}", summary="Ver empresa específica", tags=["Admin"])
def admin_get_business(
    business_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    business = db.query(Business).filter(Business.id == business_id).first()
    if not business:
        raise HTTPException(status_code=404, detail="Empresa no encontrada")
    return business_response(business, include_api_key=True)


@app.get("/admin/businesses/{business_id}/payments", summary="Pagos de una empresa", tags=["Admin"])
def admin_get_business_payments(
    business_id: str,
    status: str | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """ADMIN: ve todos los pagos de una empresa específica."""
    if not db.query(Business).filter(Business.id == business_id).first():
        raise HTTPException(status_code=404, detail="Empresa no encontrada")

    query = db.query(Payment).filter(Payment.business_id == business_id)
    if status:
        try:
            query = query.filter(Payment.status == PaymentStatus(status.upper()))
        except ValueError:
            raise HTTPException(status_code=400, detail="Estado inválido")

    payments = query.order_by(Payment.created_at.desc()).limit(limit).all()
    return [payment_to_response(p) for p in payments]


@app.get("/admin/stats", summary="Estadísticas globales de la plataforma", tags=["Admin"])
def admin_stats(
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """ADMIN: estadísticas globales de toda la plataforma."""
    total_businesses = db.query(Business).count()
    active_businesses = db.query(Business).filter(Business.is_active == True).count()
    total_payments = db.query(Payment).count()
    confirmed = db.query(Payment).filter(Payment.status == PaymentStatus.CONFIRMED).count()
    pending = db.query(Payment).filter(Payment.status == PaymentStatus.PENDING).count()
    expired = db.query(Payment).filter(Payment.status == PaymentStatus.EXPIRED).count()

    confirmed_payments = db.query(Payment).filter(Payment.status == PaymentStatus.CONFIRMED).all()
    total_amount = sum(p.paid_amount or 0 for p in confirmed_payments)

    return {
        "businesses": {
            "total": total_businesses,
            "active": active_businesses,
            "inactive": total_businesses - active_businesses,
        },
        "payments": {
            "total": total_payments,
            "confirmed": confirmed,
            "pending": pending,
            "expired": expired,
            "total_amount_confirmed_bs": round(total_amount, 2),
            "conversion_rate": f"{(confirmed / total_payments * 100):.1f}%" if total_payments > 0 else "0%",
        }
    }


@app.patch("/admin/businesses/{business_id}/toggle", summary="Activar o desactivar empresa", tags=["Admin"])
def admin_toggle_business(
    business_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """ADMIN: activa o desactiva una empresa."""
    business = db.query(Business).filter(Business.id == business_id).first()
    if not business:
        raise HTTPException(status_code=404, detail="Empresa no encontrada")

    business.is_active = not business.is_active
    db.commit()

    estado = "activada" if business.is_active else "desactivada"
    return {"message": f"Empresa '{business.name}' {estado}", "is_active": business.is_active}


# ─── CLIENT: su propia empresa ───────────────────────────────────────────────

@app.get("/businesses/me", summary="Info de tu empresa", tags=["Empresa"])
def get_my_business(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    business = db.query(Business).filter(Business.id == user.business_id).first()
    return business_response(business, include_api_key=True)


@app.put("/businesses/me/gmail", summary="Configurar Gmail de verificación", tags=["Empresa"])
def configure_gmail(
    body: GmailConfigRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    import json as _json
    try:
        _json.loads(body.credentials_json)
        _json.loads(body.token_json)
    except ValueError:
        raise HTTPException(status_code=400, detail="credentials_json o token_json no son JSON válido")

    business = db.query(Business).filter(Business.id == user.business_id).first()
    business.gmail_address = body.gmail_address
    business.gmail_credentials_json = body.credentials_json
    business.gmail_token_json = body.token_json
    db.commit()

    return {
        "message": f"Gmail '{body.gmail_address}' configurado correctamente",
        "gmail_address": business.gmail_address,
    }


@app.delete("/businesses/me/gmail", summary="Quitar Gmail de verificación", tags=["Empresa"])
def remove_gmail(
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    business = db.query(Business).filter(Business.id == user.business_id).first()
    business.gmail_address = None
    business.gmail_credentials_json = None
    business.gmail_token_json = None
    db.commit()
    return {"message": "Configuración de Gmail eliminada"}


# ─── Pagos (CLIENT) ───────────────────────────────────────────────────────────

@app.post("/payments", summary="Crear orden de pago", tags=["Pagos"])
def create_payment(
    body: CreatePaymentRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    if body.amount <= 0:
        raise HTTPException(status_code=400, detail="El monto debe ser mayor a 0")

    code = generate_payment_code()
    while db.query(Payment).filter(Payment.code == code).first():
        code = generate_payment_code()

    payment = Payment(
        code=code,
        business_id=user.business_id,
        amount=body.amount,
        description=body.description,
        webhook_url=body.webhook_url,
        expires_at=datetime.utcnow() + timedelta(minutes=body.expiry_minutes),
    )
    db.add(payment)
    db.commit()
    db.refresh(payment)
    return payment_to_response(payment, include_instructions=True)


@app.get("/payments", summary="Listar mis pagos", tags=["Pagos"])
def list_payments(
    status: str | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    query = db.query(Payment).filter(Payment.business_id == user.business_id)
    if status:
        try:
            query = query.filter(Payment.status == PaymentStatus(status.upper()))
        except ValueError:
            raise HTTPException(status_code=400, detail="Estado inválido. Usa: PENDING, CONFIRMED, EXPIRED, FAILED")

    payments = query.order_by(Payment.created_at.desc()).limit(limit).all()
    return [payment_to_response(p) for p in payments]


@app.get("/payments/{code}", summary="Consultar estado de un pago", tags=["Pagos"])
def get_payment(
    code: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    payment = db.query(Payment).filter(
        Payment.code == code,
        Payment.business_id == user.business_id,
    ).first()
    if not payment:
        raise HTTPException(status_code=404, detail=f"Pago '{code}' no encontrado")
    return payment_to_response(payment)


@app.post("/payments/{code}/verify-manual", summary="Confirmar pago manualmente", tags=["Pagos"])
def verify_manual(
    code: str,
    body: ManualVerifyRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    payment = db.query(Payment).filter(
        Payment.code == code,
        Payment.business_id == user.business_id,
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


@app.get("/stats", summary="Mis estadísticas", tags=["Pagos"])
def get_stats(
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    query = db.query(Payment).filter(Payment.business_id == user.business_id)
    total = query.count()
    confirmed = query.filter(Payment.status == PaymentStatus.CONFIRMED).count()
    pending = query.filter(Payment.status == PaymentStatus.PENDING).count()
    expired = query.filter(Payment.status == PaymentStatus.EXPIRED).count()

    confirmed_payments = db.query(Payment).filter(
        Payment.business_id == user.business_id,
        Payment.status == PaymentStatus.CONFIRMED,
    ).all()
    total_amount = sum(p.paid_amount or 0 for p in confirmed_payments)

    business = db.query(Business).filter(Business.id == user.business_id).first()
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

@app.post("/check-now", summary="Forzar revisión de emails", tags=["Sistema"])
async def check_now(background_tasks: BackgroundTasks):
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
        "service": "Verify Payments API",
        "version": "2.0.0",
        "roles": {
            "ADMIN": "Dueño de la plataforma — ve todas las empresas y pagos",
            "CLIENT": "Empresa registrada — ve solo sus propios pagos",
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

        businesses_with_gmail = db.query(Business).filter(
            Business.gmail_token_json != None,
            Business.is_active == True,
        ).all()

        for business in businesses_with_gmail:
            await _check_business_emails(db, business)
    except Exception as e:
        print(f"[JOB] Error: {e}")
    finally:
        db.close()


async def _check_business_emails(db, business: Business):
    try:
        email_payments = fetch_new_emails(
            credentials_json=business.gmail_credentials_json,
            token_json=business.gmail_token_json,
        )
        for parsed in email_payments:
            email_id = parsed.get("email_id")
            unique_id = f"{business.id}:{email_id}"
            if not email_id or is_already_processed(db, unique_id):
                continue

            payment = match_payment(db, parsed, source="email", business_id=business.id)
            if payment:
                confirmed = confirm_payment(db, payment, parsed, source="email")
                register_notification(db, unique_id, "email", str(parsed), confirmed.code)
                await send_webhook(confirmed)
                print(f"[{business.name}] ✅ {confirmed.code} — Bs. {confirmed.paid_amount}")
            else:
                register_notification(db, unique_id, "email", str(parsed))
                print(f"[{business.name}] ⚠️ Sin orden coincidente: Bs. {parsed.get('amount')}")
    except Exception as e:
        print(f"[{business.name}] Error: {e}")


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
