import os
import base64
import re
from datetime import datetime
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from dotenv import load_dotenv

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")


def get_gmail_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as token:
            token.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def clean_html(html: str) -> str:
    """Convierte HTML a texto plano limpio."""
    # Reemplazar tags de bloque con espacios
    html = re.sub(r"<br\s*/?>|<p[^>]*>|</p>|<tr[^>]*>|</tr>|<td[^>]*>|</td>", " ", html, flags=re.IGNORECASE)
    # Eliminar todos los demás tags HTML
    html = re.sub(r"<[^>]+>", "", html)
    # Decodificar entidades HTML comunes
    html = html.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    # Normalizar espacios múltiples y saltos de línea
    html = re.sub(r"[ \t]+", " ", html)
    html = re.sub(r"\n{2,}", "\n", html)
    return html.strip()


def get_email_body(message: dict) -> str:
    """Extrae y limpia el cuerpo del email."""
    payload = message.get("payload", {})

    def extract(parts):
        # Primero intentar texto plano
        for part in parts:
            if part.get("mimeType") == "text/plain":
                data = part["body"].get("data", "")
                if data:
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
        # Si no hay texto plano, buscar en partes anidadas
        for part in parts:
            if "parts" in part:
                result = extract(part["parts"])
                if result:
                    return result
        # Último recurso: HTML
        for part in parts:
            if part.get("mimeType") == "text/html":
                data = part["body"].get("data", "")
                if data:
                    html = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
                    return clean_html(html)
        return ""

    if "parts" in payload:
        return extract(payload["parts"])

    data = payload.get("body", {}).get("data", "")
    if data:
        text = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
        mime = payload.get("mimeType", "")
        if "html" in mime:
            return clean_html(text)
        return text

    return ""


def parse_mercantil_email(body: str) -> dict | None:
    """
    Parsea emails del Banco Mercantil Santa Cruz.

    Tipos de transferencias que detecta:
    - Crédito Transferencia QR
    - Crédito por transferencia
    - Cualquier crédito recibido en cuenta

    Ejemplo del email:
    "Banco Mercantil Santa Cruz S.A. le informa que se ha realizado la siguiente transacción:
     Crédito Transferencia QR, por concepto de transferencia, a su cuenta 1033347298
     de la cuenta 10000055856345 de CRUZ MENDEZ CARLOS ENRIQUE del BANCO UNION S.A.,
     por un monto de Bs 300.00.
     La transacción fue realizada el 03/05/2026 a las 08:23:44 pm.
     número de notificación: NTR-24098597."
    """

    # Normalizar texto: quitar espacios extra
    body_clean = re.sub(r"\s+", " ", body)

    # Solo procesar si es un CRÉDITO (transferencia recibida)
    if not re.search(r"cr[eé]dito", body_clean, re.IGNORECASE):
        return None

    result = {}

    # --- MONTO ---
    # Formatos: "Bs 300.00" / "Bs. 300.00" / "monto de Bs 300.00"
    amount_match = re.search(r"monto de Bs\.?\s*([\d\.]+)", body_clean, re.IGNORECASE)
    if not amount_match:
        amount_match = re.search(r"Bs\.?\s*([\d\.]+)", body_clean, re.IGNORECASE)
    if not amount_match:
        return None
    try:
        result["amount"] = float(amount_match.group(1).replace(",", ""))
    except ValueError:
        return None

    # --- NOMBRE DEL PAGADOR ---
    # Formato: "de NOMBRE APELLIDO del BANCO" o "de NOMBRE APELLIDO de BANCO"
    name_match = re.search(
        r"de\s+([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ\s]{5,50}?)\s+del?\s+BANCO",
        body_clean,
        re.IGNORECASE
    )
    if name_match:
        result["payer_name"] = name_match.group(1).strip()

    # --- NÚMERO DE NOTIFICACIÓN ---
    # Formato: "NTR-24098597"
    ntr_match = re.search(r"(NTR-\d+)", body_clean, re.IGNORECASE)
    if ntr_match:
        result["transaction_id"] = ntr_match.group(1)

    # --- BANCO ORIGEN ---
    bank_match = re.search(r"del?\s+(BANCO\s+[\w\s]+?)(?:\s+S\.A\.|\s+S\.A|[,\.])", body_clean, re.IGNORECASE)
    if bank_match:
        result["payer_bank"] = bank_match.group(1).strip()

    # --- CUENTA ORIGEN ---
    account_match = re.search(r"de la cuenta\s+(\d+)", body_clean, re.IGNORECASE)
    if account_match:
        result["payer_account"] = account_match.group(1)

    # --- CÓDIGO DE PAGO PAY-XXXXXX ---
    # El cliente debe escribir esto en el concepto/glosa de la transferencia
    code_match = re.search(r"(PAY-\d{4,8})", body_clean, re.IGNORECASE)
    if code_match:
        result["payment_code"] = code_match.group(1).upper()

    # --- FECHA DE TRANSACCIÓN ---
    date_match = re.search(r"(\d{2}/\d{2}/\d{4})\s+a las\s+(\d{2}:\d{2}:\d{2})", body_clean)
    if date_match:
        result["transaction_date"] = f"{date_match.group(1)} {date_match.group(2)}"

    # --- TIPO DE TRANSFERENCIA ---
    if re.search(r"transferencia qr", body_clean, re.IGNORECASE):
        result["transfer_type"] = "QR"
    elif re.search(r"transferencia", body_clean, re.IGNORECASE):
        result["transfer_type"] = "TRANSFERENCIA"
    else:
        result["transfer_type"] = "CREDITO"

    return result


def fetch_new_tigo_emails(last_check_timestamp: int = None) -> list[dict]:
    """
    Busca emails nuevos del Banco Mercantil Santa Cruz desde Gmail.
    """
    try:
        service = get_gmail_service()

        # Filtrar solo emails de notificaciones del Mercantil
        query = "from:bancomercantil subject:Notificaciones"
        if last_check_timestamp:
            query += f" after:{last_check_timestamp}"

        results = service.users().messages().list(
            userId="me",
            q=query,
            maxResults=20
        ).execute()

        messages = results.get("messages", [])
        parsed_payments = []

        for msg_ref in messages:
            msg_id = msg_ref["id"]
            message = service.users().messages().get(
                userId="me",
                id=msg_id,
                format="full"
            ).execute()

            body = get_email_body(message)
            parsed = parse_mercantil_email(body)

            if parsed:
                parsed["email_id"] = msg_id
                parsed["source"] = "email"
                parsed["received_at"] = datetime.utcnow().isoformat()
                parsed_payments.append(parsed)
                print(f"[EMAIL] ✅ Pago detectado: Bs. {parsed['amount']} de {parsed.get('payer_name', 'Desconocido')} ({parsed.get('transfer_type', '')})")
            else:
                print(f"[EMAIL] Email de Mercantil ignorado (no es crédito o no se pudo parsear)")

        return parsed_payments

    except Exception as e:
        print(f"[EMAIL] Error leyendo Gmail: {e}")
        return []
