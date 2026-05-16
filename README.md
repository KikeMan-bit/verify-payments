# 🇧🇴 Tigo Money Payment Verifier API

API para verificar pagos de Tigo Money en Bolivia mediante parsing de emails y SMS.

---

## 📁 Estructura del proyecto

```
tigo-payment-api/
├── main.py              # API principal con todos los endpoints
├── email_reader.py      # Lee y parsea emails de Gmail
├── sms_reader.py        # Lee SMS via Twilio
├── payment_checker.py   # Lógica de verificación y match
├── database.py          # Modelos y conexión PostgreSQL
├── requirements.txt     # Dependencias
├── .env.example         # Variables de entorno (copia a .env)
└── credentials.json     # Credenciales Gmail (NO subir a git)
```

---

## ⚙️ Instalación paso a paso

### 1. Clonar e instalar dependencias

```bash
git clone <tu-repo>
cd tigo-payment-api

python -m venv venv
source venv/bin/activate        # Linux/Mac
# venv\Scripts\activate         # Windows

pip install -r requirements.txt
```

### 2. Configurar variables de entorno

```bash
cp .env.example .env
```

Edita `.env` con tus datos:

```env
DATABASE_URL=postgresql://usuario:password@localhost:5432/tigo_payments
GMAIL_ADDRESS=tu_correo@gmail.com
API_SECRET_KEY=una_clave_muy_segura_aqui
```

### 3. Crear base de datos PostgreSQL

```sql
CREATE DATABASE tigo_payments;
```

Las tablas se crean automáticamente al iniciar la API.

### 4. Configurar Gmail API (para leer emails)

1. Ve a https://console.cloud.google.com
2. Crea un nuevo proyecto
3. Busca "Gmail API" y habilítala
4. Ve a "Credenciales" → "Crear credenciales" → "ID de cliente OAuth 2.0"
5. Tipo de aplicación: **Aplicación de escritorio**
6. Descarga el JSON y renómbralo como `credentials.json`
7. Colócalo en la raíz del proyecto
8. La primera vez que inicies la API, abrirá el navegador para autorizar acceso

### 5. Configurar Twilio para SMS (opcional)

1. Crea cuenta en https://twilio.com
2. Compra un número telefónico que reciba SMS
3. Configura ese número para recibir los SMS de Tigo Money (redirección)
4. Copia tus credenciales en `.env`

> **Alternativa sin Twilio:** Puedes usar un celular Android + ADB para reenviar SMS a tu API. Consulta la sección avanzada más abajo.

### 6. Iniciar la API

```bash
uvicorn main:app --reload --port 8000
```

La API estará disponible en: http://localhost:8000

Documentación automática en: http://localhost:8000/docs

---

## 🔌 Endpoints

Todos los endpoints requieren el header:
```
X-Api-Key: tu_clave_secreta
```

### POST /payments — Crear orden de pago

```bash
curl -X POST http://localhost:8000/payments \
  -H "X-Api-Key: tu_clave" \
  -H "Content-Type: application/json" \
  -d '{
    "business_id": "tienda-abc",
    "amount": 150.00,
    "description": "Pedido #1234",
    "webhook_url": "https://tu-tienda.com/webhook/pago"
  }'
```

**Respuesta:**
```json
{
  "code": "PAY-485921",
  "amount": 150.00,
  "status": "PENDING",
  "instructions": {
    "message": "El pagador debe escribir 'PAY-485921' en el concepto del pago",
    "amount": "Bs. 150.00",
    "reference_code": "PAY-485921",
    "expires_at": "2024-01-15T14:30:00"
  }
}
```

### GET /payments/{code} — Consultar estado

```bash
curl http://localhost:8000/payments/PAY-485921 \
  -H "X-Api-Key: tu_clave"
```

**Respuesta cuando está confirmado:**
```json
{
  "code": "PAY-485921",
  "status": "CONFIRMED",
  "paid_amount": 150.00,
  "payer_name": "JUAN PEREZ",
  "payer_phone": "73456789",
  "transaction_id": "TG-987654",
  "detection_source": "sms",
  "confirmed_at": "2024-01-15T14:05:23"
}
```

### GET /payments — Listar pagos de un negocio

```bash
curl "http://localhost:8000/payments?business_id=tienda-abc&status=CONFIRMED" \
  -H "X-Api-Key: tu_clave"
```

### POST /payments/{code}/verify-manual — Confirmar manualmente

Cuando el cliente no escribió el código en el concepto:

```bash
curl -X POST http://localhost:8000/payments/PAY-485921/verify-manual \
  -H "X-Api-Key: tu_clave" \
  -H "Content-Type: application/json" \
  -d '{
    "amount": 150.00,
    "payer_name": "JUAN PEREZ",
    "payer_phone": "73456789",
    "transaction_id": "TG-987654"
  }'
```

### POST /check-now — Forzar revisión

```bash
curl -X POST http://localhost:8000/check-now \
  -H "X-Api-Key: tu_clave"
```

### GET /stats — Estadísticas

```bash
curl "http://localhost:8000/stats?business_id=tienda-abc" \
  -H "X-Api-Key: tu_clave"
```

---

## 💡 Flujo para el cliente final

Cuando alguien quiere pagarte:

1. Tu sistema llama `POST /payments` con el monto
2. Obtienes el código `PAY-XXXXXX`
3. Le muestras al cliente:
   ```
   Paga Bs. 150.00 a [tu número Tigo Money]
   ⚠️ IMPORTANTE: Escribe PAY-485921 en el concepto
   ```
4. El cliente paga desde su app Tigo Money
5. Tigo envía un email/SMS de confirmación
6. Tu API lo detecta en ~10-30 segundos
7. Llamas `GET /payments/PAY-485921` y ves `"status": "CONFIRMED"`
8. O recibes el webhook automáticamente en tu URL

---

## ⚠️ Ajustar los patrones de parseo

El archivo `email_reader.py` y `sms_reader.py` contienen expresiones regulares 
para extraer datos del email/SMS de Tigo Money.

**IMPORTANTE:** Debes ajustar estos patrones según el formato real de tus 
notificaciones. Para hacerlo:

1. Haz una transferencia de prueba a tu cuenta Tigo Money
2. Anota el texto exacto del email y SMS que recibiste
3. Ajusta los patrones en `parse_tigo_email()` y `parse_tigo_sms()`

---

## 🚀 Despliegue en producción

### Con Docker

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### Con systemd (Linux)

```ini
[Unit]
Description=Tigo Payment API
After=network.target

[Service]
WorkingDirectory=/home/tu-usuario/tigo-payment-api
ExecStart=/home/tu-usuario/tigo-payment-api/venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

---

## 📊 Estados de un pago

| Estado | Descripción |
|---|---|
| `PENDING` | Esperando el pago |
| `CONFIRMED` | Pago detectado y verificado |
| `EXPIRED` | Venció el tiempo límite |
| `FAILED` | Error en la verificación |
