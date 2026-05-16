from datetime import datetime
from sqlalchemy.orm import Session
import httpx

from database import Payment, PaymentStatus, ProcessedNotification


AMOUNT_TOLERANCE = 0.01  # Tolerancia de 1 centavo por redondeo


def match_payment(db: Session, parsed: dict, source: str, business_id: str = None) -> Payment | None:
    """
    Intenta hacer match entre un pago detectado (email/SMS) y una orden pendiente.
    Estrategia:
    1. Si el parsed tiene payment_code → busca directo
    2. Si no → busca por monto + estado PENDING
    Si se pasa business_id, solo busca dentro de esa empresa.
    """
    payment = None

    base_query = db.query(Payment).filter(Payment.status == PaymentStatus.PENDING)
    if business_id:
        base_query = base_query.filter(Payment.business_id == business_id)

    # Estrategia 1: Buscar por código en el concepto
    if "payment_code" in parsed:
        payment = base_query.filter(Payment.code == parsed["payment_code"]).first()

    # Estrategia 2: Buscar por monto exacto (menos confiable)
    if not payment and "amount" in parsed:
        amount = parsed["amount"]
        payment = base_query.filter(
            Payment.amount >= amount - AMOUNT_TOLERANCE,
            Payment.amount <= amount + AMOUNT_TOLERANCE,
        ).order_by(Payment.created_at.asc()).first()

    return payment


def confirm_payment(db: Session, payment: Payment, parsed: dict, source: str) -> Payment:
    """Marca el pago como confirmado y guarda los datos del pagador."""
    payment.status = PaymentStatus.CONFIRMED
    payment.confirmed_at = datetime.utcnow()
    payment.payer_name = parsed.get("payer_name")
    payment.payer_phone = parsed.get("payer_phone")
    payment.paid_amount = parsed.get("amount")
    payment.transaction_id = parsed.get("transaction_id")
    payment.detection_source = source
    db.commit()
    db.refresh(payment)
    return payment


def register_notification(db: Session, notification_id: str, source: str, raw: str, code: str = None):
    """Guarda la notificación procesada para evitar duplicados."""
    existing = db.query(ProcessedNotification).filter(
        ProcessedNotification.notification_id == notification_id
    ).first()

    if not existing:
        notif = ProcessedNotification(
            notification_id=notification_id,
            source=source,
            raw_content=raw[:1000],
            payment_code=code,
        )
        db.add(notif)
        db.commit()


def is_already_processed(db: Session, notification_id: str) -> bool:
    """Verifica si ya se procesó esta notificación."""
    return db.query(ProcessedNotification).filter(
        ProcessedNotification.notification_id == notification_id
    ).first() is not None


def expire_old_payments(db: Session) -> int:
    """Marca como expiradas las órdenes que superaron su tiempo límite."""
    now = datetime.utcnow()
    expired = db.query(Payment).filter(
        Payment.status == PaymentStatus.PENDING,
        Payment.expires_at < now
    ).all()

    for p in expired:
        p.status = PaymentStatus.EXPIRED

    db.commit()
    return len(expired)


async def send_webhook(payment: Payment):
    """Notifica al negocio via webhook cuando se confirma un pago."""
    if not payment.webhook_url:
        return

    payload = {
        "event": "payment.confirmed",
        "payment_code": payment.code,
        "amount": payment.paid_amount,
        "payer_name": payment.payer_name,
        "payer_phone": payment.payer_phone,
        "transaction_id": payment.transaction_id,
        "detection_source": payment.detection_source,
        "confirmed_at": payment.confirmed_at.isoformat() if payment.confirmed_at else None,
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(payment.webhook_url, json=payload)
            print(f"[WEBHOOK] Notificado: {payment.code} → {payment.webhook_url}")
    except Exception as e:
        print(f"[WEBHOOK] Error notificando {payment.code}: {e}")
