import re


def parse_tigo_sms(body: str) -> dict | None:
    """
    Extrae los datos del SMS de confirmación de Tigo Money.

    Ejemplo de SMS típico de Tigo Money Bolivia:
    "Recibiste Bs. 150.00 de JUAN PEREZ 73456789. Ref: TG-987654. Saldo: Bs. 500.00"

    ⚠️ Ajusta los patrones según el SMS real que recibas de Tigo.
    """
    patterns = {
        "amount": [
            r"Bs\.?\s*([\d,\.]+)",
            r"recibiste\s+Bs\.?\s*([\d,\.]+)",
        ],
        "payer_name": [
            r"de\s+([A-ZÁÉÍÓÚÑ][A-Za-záéíóúÁÉÍÓÚñÑ\s]+?)[\s\d]",
            r"remitente[:\s]+([A-Za-záéíóúÁÉÍÓÚñÑ\s]+?)[\.\n]",
        ],
        "payer_phone": [
            r"(\+?591\s?[67]\d{7})",
            r"\b([67]\d{7})\b",
        ],
        "transaction_id": [
            r"Ref[:\s]+([A-Z0-9\-]+)",
            r"referencia[:\s]+([A-Z0-9\-]+)",
            r"TG-(\d+)",
        ],
        "payment_code": [
            r"(PAY-\d{4,8})",
        ],
    }

    result = {}

    for field, field_patterns in patterns.items():
        for pattern in field_patterns:
            match = re.search(pattern, body, re.IGNORECASE)
            if match:
                value = match.group(1).strip()
                if field == "amount":
                    value = float(value.replace(",", "."))
                result[field] = value
                break

    if "amount" not in result:
        return None

    return result
