import os
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from portal_auth import (
    CLIENT_AUTH_COOKIE, CLIENT_SESSION_SECONDS, AuthStore,
    RESIDENCE_NAMES,
)
from resident_store import ResidentStore, payment_status


CLIENT_PWA_DIR = Path(__file__).with_name("client_pwa")
COOKIE_SECURE = os.environ.get("CLIENT_COOKIE_SECURE", "true").lower() == "true"

app = FastAPI(title="Residencias - Portal de clientes", docs_url=None, redoc_url=None)
app.mount("/assets", StaticFiles(directory=CLIENT_PWA_DIR / "assets"), name="assets")


class LoginBody(BaseModel):
    username: str = Field(min_length=3, max_length=40)
    password: str = Field(min_length=1, max_length=128)


class PasswordBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=12, max_length=128)


def _client_access(request: Request, *, allow_password_change: bool = False):
    token = request.cookies.get(CLIENT_AUTH_COOKIE, "")
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Inicia sesión.")
    auth = AuthStore()
    try:
        user = auth.validate(token)
    except PermissionError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="La sesión venció.",
        ) from error
    if user["role"] != "viewer" or user["resident_id"] is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Esta cuenta no está configurada como cuenta de cliente.",
        )
    if user["must_change"] and not allow_password_change:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "password_change_required", "message": "Cambia tu contraseña."},
        )
    return auth, token, user


def _resident_for_user(user):
    resident = _resident_store().get_resident(user["resident_id"])
    if resident is None or resident.client_key != user["resident_key"]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cuenta sin residente asociada.")
    return resident


def _resident_store() -> ResidentStore:
    data_dir = Path(os.environ.get("RESIDENCIAS_DATA_DIR", Path(__file__).parent / "data"))
    return ResidentStore(data_dir / "residencias.sqlite3")


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; manifest-src 'self'; object-src 'none'; "
        "base-uri 'self'; frame-ancestors 'none'"
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
def index():
    return FileResponse(CLIENT_PWA_DIR / "index.html")


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(
        CLIENT_PWA_DIR / "manifest.webmanifest",
        media_type="application/manifest+json",
    )


@app.get("/service-worker.js")
def service_worker():
    return FileResponse(
        CLIENT_PWA_DIR / "service-worker.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"},
    )


@app.post("/api/login")
def login(body: LoginBody, response: Response):
    auth = AuthStore()
    try:
        token = auth.login(body.username, body.password)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Usuario o contraseña incorrectos, cuenta inactiva o temporalmente bloqueada.",
        ) from error
    try:
        user = auth.validate(token)
    except PermissionError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="La sesión venció.",
        ) from error
    if user["role"] != "viewer" or user["resident_id"] is None:
        auth.logout(token)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Esta cuenta no tiene acceso al portal de clientes.",
        )
    response.set_cookie(
        CLIENT_AUTH_COOKIE,
        token,
        max_age=CLIENT_SESSION_SECONDS,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="strict",
        path="/",
    )
    return {"username": user["username"], "must_change_password": bool(user["must_change"])}


@app.delete("/api/login", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response):
    token = request.cookies.get(CLIENT_AUTH_COOKIE, "")
    if token:
        AuthStore().logout(token)
    response.delete_cookie(
        CLIENT_AUTH_COOKIE, path="/", secure=COOKIE_SECURE, httponly=True, samesite="strict",
    )


@app.get("/api/session")
def session(request: Request):
    _, _, user = _client_access(request, allow_password_change=True)
    return {
        "username": user["username"],
        "must_change_password": bool(user["must_change"]),
    }


@app.post("/api/password")
def change_password(body: PasswordBody, request: Request, response: Response):
    auth, token, _ = _client_access(request, allow_password_change=True)
    try:
        auth.change_password(token, body.current_password, body.new_password)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    response.delete_cookie(
        CLIENT_AUTH_COOKIE, path="/", secure=COOKIE_SECURE, httponly=True, samesite="strict",
    )
    return {"changed": True, "sign_in_again": True}


@app.get("/api/client/summary")
def client_summary(request: Request):
    _, _, user = _client_access(request)
    resident = _resident_for_user(user)
    return {
        "name": resident.values.get("student_name", ""),
        "residence": RESIDENCE_NAMES[resident.residence],
        "room": resident.values.get("room_number", ""),
        "bed": resident.values.get("bed_number", ""),
        "active": resident.discharged_on is None,
        "admission_date": resident.admission_date.isoformat(),
        "next_payment": resident.next_payment.isoformat(),
        "payment_status": payment_status(resident.next_payment, date.today()),
        "discharged_on": (
            resident.discharged_on.isoformat() if resident.discharged_on else None
        ),
    }


@app.get("/api/client/payments")
def client_payments(request: Request):
    _, _, user = _client_access(request)
    resident = _resident_for_user(user)
    store = _resident_store()
    payments = [
        {
            "kind": "Mensualidad",
            "due_date": item["due_date"],
            "paid_on": item["paid_on"],
            "amount": item["amount"],
            "currency": item["currency"],
            "payment_method": item["payment_method"],
            "receipt_url": f"/api/client/receipts/monthly/{item['due_date']}",
        }
        for item in store.payment_history(resident.id, resident.residence)
    ]
    deposit = store.deposit_details(resident.id, resident.residence)
    if deposit:
        payments.append({
            "kind": "Depósito de ingreso",
            "due_date": deposit.paid_on,
            "paid_on": deposit.paid_on,
            "amount": deposit.amount,
            "currency": deposit.currency,
            "payment_method": deposit.payment_method,
            "receipt_url": "/api/client/receipts/deposit",
        })
    for item in store.service_payment_history(resident.id, resident.residence):
        payments.append({
            "kind": item["concept"],
            "due_date": item["paid_on"],
            "paid_on": item["paid_on"],
            "amount": item["amount"],
            "currency": item["currency"],
            "payment_method": item["payment_method"],
            "receipt_url": f"/api/client/receipts/service/{item['id']}",
        })
    return sorted(payments, key=lambda payment: payment["paid_on"], reverse=True)


@app.get("/api/client/documents")
def client_documents(request: Request):
    _, _, user = _client_access(request)
    resident = _resident_for_user(user)
    store = _resident_store()
    documents = [{
        "name": "Planilla de ingreso",
        "url": "/api/client/documents/admission",
    }]
    if store.get_deposit_receipt(resident.id, resident.residence):
        documents.append({
            "name": "Recibo de depósito",
            "url": "/api/client/receipts/deposit",
        })
    documents.extend(
        {
            "name": f"Recibo de mensualidad — {item['due_date']}",
            "url": f"/api/client/receipts/monthly/{item['due_date']}",
        }
        for item in store.payment_history(resident.id, resident.residence)
    )
    documents.extend(
        {
            "name": f"Recibo — {item['concept']} ({item['paid_on']})",
            "url": f"/api/client/receipts/service/{item['id']}",
        }
        for item in store.service_payment_history(resident.id, resident.residence)
    )
    return documents


def _pdf_response(pdf: bytes | None, filename: str):
    if pdf is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Documento no encontrado.")
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/client/documents/admission")
def admission_document(request: Request):
    _, _, user = _client_access(request)
    resident = _resident_for_user(user)
    pdf = _resident_store().get_pdf(resident.id, resident.residence)
    return _pdf_response(pdf, "planilla-ingreso.pdf")


@app.get("/api/client/receipts/monthly/{due_date}")
def monthly_receipt(due_date: date, request: Request):
    _, _, user = _client_access(request)
    resident = _resident_for_user(user)
    pdf = _resident_store().get_payment_receipt(
        resident.id, resident.residence, due_date,
    )
    return _pdf_response(pdf, f"recibo-mensualidad-{due_date.isoformat()}.pdf")


@app.get("/api/client/receipts/deposit")
def deposit_receipt(request: Request):
    _, _, user = _client_access(request)
    resident = _resident_for_user(user)
    pdf = _resident_store().get_deposit_receipt(resident.id, resident.residence)
    return _pdf_response(pdf, "recibo-deposito.pdf")


@app.get("/api/client/receipts/service/{service_payment_id}")
def service_receipt(service_payment_id: int, request: Request):
    _, _, user = _client_access(request)
    resident = _resident_for_user(user)
    pdf = _resident_store().get_service_receipt(
        resident.id, resident.residence, service_payment_id,
    )
    return _pdf_response(pdf, "recibo-servicio.pdf")


@app.get("/api/client/notifications")
def client_notifications(request: Request):
    _client_access(request)
    return _resident_store().client_notifications()


@app.get("/api/client/expenses")
def client_expenses(request: Request):
    _client_access(request)
    return [
        {
            "concept": item["payload"]["concept"],
            "paid_on": item["payload"]["paid_on"],
            "currency": item["payload"]["currency"],
            "amount": item["payload"]["amount"],
            "residence": RESIDENCE_NAMES[item["payload"]["residence"]],
            "created_at": item["created_at"],
        }
        for item in _resident_store().client_notifications()
        if item["kind"] == "expense"
    ]
