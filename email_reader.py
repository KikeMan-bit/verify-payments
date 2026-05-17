import os
import base64
import json
import re
from datetime import datetime
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from dotenv import load_dotenv

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def get_gmail_service(credentials_json: str = None, token_json: str = None):
    """
    Devuelve un servicio Gmail autenticado.
    Si se pasan credentials_json/token_json (strings), los usa directamente.
    Si no, cae al comportamiento legacy (archivos del .env).
    """
    creds = None

    if token_json:
        creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
    else:
        token_file = os.getenv("GMAIL_TOKEN_FILE", "token.json")
        if os.path.exists(token_file):
            creds = Credentials.from_authorized_user_file(token_file, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        elif credentials_json:
            # No se puede hacer el flujo interactivo via API — el token debe estar pre-generado
            raise ValueError(
                "El token de Gmail está vencido o es inválido y no puede renovarse automáticamente. "
                "Sube un nuevo token_json válido via PUT /businesses/me/gmail"
            )
        else:
            creds_file = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
            flow = InstalledAppFlow.from_client_secrets_file(creds_file, SCOPES)
            creds = flow.run_local_server(port=0)
            token_file = os.getenv("GMAIL_TOKEN_FILE", "token.json")
            with open(token_file, "w") as f:
                f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def clean_html(html: str) -> str:
    html = re.sub(r"<br\s*/?>|<p[^>]*>|</p>|<tr[^>]*>|</tr>|<td[^>]*>|</td>", " ", html, flags=re.IGNORECASE)
    html = re.sub(r"<[^>]+>", "", html)
    html = html.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    html = re.sub(r"[ \t]+", " ", html)
    html = re.sub(r"\n{2,}", "\n", html)
    return html.strip()


def get_email_body(message: dict) -> str:
    payload = message.get("payload", {})

    def extract(parts):
        for part in parts:
            if part.get("mimeType") == "text/plain":
                data = part["body"].get("data", "")
                if data:
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
        for part in parts:
            if "parts" in part:
                result = extract(part["parts"])
                if result:
                    return result
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
    Detecta créditos (transferencias recibidas) y extrae monto, pagador y referencia.
    """
    body_clean = re.sub(r"\s+", " ", body)

    if not re.search(r"cr[eé]dito", body_clean, re.IGNORECASE):
        return None

    result = {}

    amount_match = re.search(r"monto de Bs\.?\s*([\d\.]+)", body_clean, re.IGNORECASE)
    if not amount_match:
        amount_match = re.search(r"Bs\.?\s*([\d\.]+)", body_clean, re.IGNORECASE)
    if not amount_match:
        return None
    try:
        result["amount"] = float(amount_match.group(1).replace(",", ""))
    except ValueError:
        return None

    name_match = re.search(
        r"de\s+([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ\s]{5,50}?)\s+del?\s+BANCO",
        body_clean,
        re.IGNORECASE
    )
    if name_match:
        result["payer_name"] = name_match.group(1).strip()

    ntr_match = re.search(r"(NTR-\d+)", body_clean, re.IGNORECASE)
    if ntr_match:
        result["transaction_id"] = ntr_match.group(1)

    bank_match = re.search(r"del?\s+(BANCO\s+[\w\s]+?)(?:\s+S\.A\.|\s+S\.A|[,\.])", body_clean, re.IGNORECASE)
    if bank_match:
        result["payer_bank"] = bank_match.group(1).strip()

    account_match = re.search(r"de la cuenta\s+(\d+)", body_clean, re.IGNORECASE)
    if account_match:
        result["payer_account"] = account_match.group(1)

    code_match = re.search(r"(PAY-\d{4,8})", body_clean, re.IGNORECASE)
    if code_match:
        result["payment_code"] = code_match.group(1).upper()

    date_match = re.search(r"(\d{2}/\d{2}/\d{4})\s+a las\s+(\d{2}:\d{2}:\d{2})", body_clean)
    if date_match:
        result["transaction_date"] = f"{date_match.group(1)} {date_match.group(2)}"

    if re.search(r"transferencia qr", body_clean, re.IGNORECASE):
        result["transfer_type"] = "QR"
    elif re.search(r"transferencia", body_clean, re.IGNORECASE):
        result["transfer_type"] = "TRANSFERENCIA"
    else:
        result["transfer_type"] = "CREDITO"

    return result


def fetch_new_emails(
    last_check_timestamp: int = None,
    credentials_json: str = None,
    token_json: str = None,
) -> list[dict]:
    """
    Busca emails nuevos del Banco Mercantil Santa Cruz desde Gmail.
    Acepta credenciales por empresa; si no se pasan, usa el Gmail global del .env.
    """
    try:
        service = get_gmail_service(credentials_json=credentials_json, token_json=token_json)

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
                print(f"[EMAIL] ✅ Pago detectado: Bs. {parsed['amount']} de {parsed.get('payer_name', 'Desconocido')}")
            else:
                print(f"[EMAIL] Email de Mercantil ignorado (no es crédito o no se pudo parsear)")

        return parsed_payments

    except Exception as e:
        print(f"[EMAIL] Error leyendo Gmail: {e}")
        return []
