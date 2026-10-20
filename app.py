import json
import logging
import sqlite3
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pymupdf
import streamlit as st

from deposit_receipt import SERVICE_CONCEPTS, calculate_receipt_vat, format_bolivars
from monthly_report import generate_monthly_report
from payment_reminders import MailConfig, VENEZUELA
from resident_store import (
    ALERT_DAYS, BANKS, ResidentStore, bolivars_from_dollars, calculate_age,
    normalize_admission_values, payment_status,
)

LOGGER = logging.getLogger(__name__)
ADMISSION_TEMPLATE = Path(__file__).with_name(
    "Planilla_de_Ingreso_Residencia_Carmen_Luisa.pdf"
)
LOCATION_CATALOG = json.loads(
    Path(__file__).with_name("venezuela_locations.json").read_text(encoding="utf-8")
)["states"]
VENEZUELAN_STATES = sorted(state.upper() for state in LOCATION_CATALOG)
VENEZUELAN_CITIES = sorted({
    city.upper() for cities in LOCATION_CATALOG.values() for city in cities
})
DOCUMENT_LABELS = [
    "Ser mayor de edad",
    "Certificado de antecedentes penales",
    "Constancia de estudios o inscripción universitaria",
    "Cédula de los padres o representante legal",
    "Foto tipo carnet reciente",
    "Dos referencias personales del representante",
    "Cédula de identidad de la estudiante",
    "Constancia de trabajo de los padres o representante",
]
PERSONAL_FIELDS = {
    "student_name": "Nombres y apellidos",
    "student_id": "Cédula de identidad",
    "birth_date": "Fecha de nacimiento",
    "age": "Edad",
    "student_mobile": "Teléfono celular",
    "student_landline": "Teléfono fijo",
    "emergency_phone": "Teléfono de emergencia",
    "email": "Correo electrónico",
    "student_address": "Dirección actual",
    "city": "Ciudad",
    "state": "Estado",
    "mother_name": "Nombre de la madre / tutora",
    "mother_phone": "Teléfono celular / trabajo (madre o tutora)",
    "mother_workplace": "Lugar de trabajo (madre o tutora)",
    "mother_landline": "Teléfono fijo (madre o tutora)",
    "mother_address": "Dirección (madre o tutora)",
    "father_name": "Nombre del padre / tutor",
    "father_phone": "Teléfono celular / trabajo (padre o tutor)",
    "father_workplace": "Lugar de trabajo (padre o tutor)",
    "father_landline": "Teléfono fijo (padre o tutor)",
    "father_address": "Dirección (padre o tutor)",
    "emergency_contacts": "Contactos de emergencia",
    "representative_id": "Cédula de la madre",
    "admission_date": "Fecha de ingreso",
    "room_number": "Habitación",
    "bed_number": "Cama",
}


def _insert_pdf_value(page, value, rect, font_size, cover_underline=False):
    value = " ".join(value.split())
    if not value:
        return

    text_rect = pymupdf.Rect(rect)
    if cover_underline:
        page.draw_rect(text_rect, color=None, fill=(1, 1, 1))

    if pymupdf.get_text_length(value, fontname="helv", fontsize=font_size) > text_rect.width:
        raise ValueError("Uno de los datos es demasiado largo para el espacio disponible.")
    _insert_student_value(
        page, value, text_rect.x0, text_rect.y0 + 1.075 * font_size, font_size
    )


def _insert_student_value(page, value, x, baseline, font_size):
    value = " ".join(value.split())
    if not value:
        return

    page.insert_text(
        (x, baseline),
        value,
        fontname="helv",
        fontsize=font_size,
        color=(0, 0, 0),
    )


def _wrap_pdf_value(value, width, continuation_width=None):
    lines = []
    line = ""
    for character in " ".join(value.split()):
        if pymupdf.get_text_length(line + character, fontname="helv", fontsize=10) > width:
            split_at = line.rfind(" ")
            if split_at > 0:
                lines.append(line[:split_at])
                line = line[split_at + 1:]
            else:
                lines.append(line)
                line = ""
            if continuation_width is not None:
                width = continuation_width
        line += character
    if line.strip():
        lines.append(line.strip())
    return lines


def _insert_admission_photo(page, photo_bytes):
    frame = pymupdf.Rect(453.94, 43.92, 561.24, 134.30)
    with pymupdf.open(stream=photo_bytes) as image:
        with pymupdf.open("pdf", image.convert_to_pdf()) as photo:
            source = photo[0].rect
            aspect_ratio = frame.width / frame.height
            if source.width / source.height > aspect_ratio:
                width = source.height * aspect_ratio
                crop = pymupdf.Rect(
                    (source.width - width) / 2, 0,
                    (source.width + width) / 2, source.height,
                )
            else:
                height = source.width / aspect_ratio
                crop = pymupdf.Rect(
                    0, (source.height - height) / 2,
                    source.width, (source.height + height) / 2,
                )
            page.show_pdf_page(frame, photo, clip=crop)


def _generate_admission_pdf(values, submitted_documents, photo_bytes):
    values = normalize_admission_values(values)
    with pymupdf.open(ADMISSION_TEMPLATE) as document:
        page = document[0]
        student_placements = {
            "student_name": (162.5, 312.22, 199.6),
            "student_id": (483.5, 312.22, 77.1),
            "birth_date": (115.8, 333.94, 54.9),
            "age": (174, 333.94, 51),
            "student_mobile": (294.3, 333.94, 67.8),
            "student_landline": (412.8, 333.94, 147.8),
            "emergency_phone": (137.3, 355.66, 90.1),
            "email": (265.2, 355.66, 295.4),
            "student_address": (180.5, 377.38, 380.1),
            "city": (94, 420.84, 212),
            "state": (349, 420.84, 211.6),
        }
        placements = {
            "mother_name": (194, 458, 347, 476),
            "mother_phone": (479, 458, 559, 476),
            "mother_workplace": (140, 480, 347, 498),
            "mother_landline": (450, 480, 559, 498),
            "mother_address": (207, 501, 559, 520),
            "father_name": (179, 523, 347, 542),
            "father_phone": (479, 523, 559, 542),
            "father_workplace": (140, 545, 347, 563),
            "father_landline": (450, 545, 559, 563),
            "father_address": (207, 567, 559, 585),
            "emergency_contacts": (337, 589, 559, 607),
        }
        signature_placements = {
            "student_id": (129, 379, 261, 393),
            "representative_id": (384, 379, 518, 393),
            "admission_date": (130, 505, 259, 520),
            "room_number": (273, 569, 312, 584),
            "bed_number": (367, 569, 406, 584),
        }
        font_size = 10
        annex = {}
        page.draw_rect(
            pymupdf.Rect(172, 320, 226, 338), color=None, fill=(1, 1, 1)
        )

        def fitted_value(field, width):
            value = " ".join(values.get(field, "").split())
            if pymupdf.get_text_length(value, fontname="helv", fontsize=10) <= width:
                return value
            if field not in annex:
                annex[field] = (len(annex) + 1, value)
            reference = f"Anexo {annex[field][0]}"
            if pymupdf.get_text_length(reference, fontname="helv", fontsize=10) > width:
                raise ValueError(
                    f"El dato «{PERSONAL_FIELDS[field]}» no cabe en su casilla "
                    "con letra de 10 puntos."
                )
            return reference

        for field_name, (x, baseline, width) in student_placements.items():
            if field_name == "student_address":
                continuation_x = 53.664
                lines = _wrap_pdf_value(
                    values.get(field_name, ""), width,
                    continuation_width=x + width - continuation_x,
                )
                if len(lines) <= 2:
                    for index, line in enumerate(lines):
                        _insert_student_value(
                            page, line, x if index == 0 else continuation_x,
                            baseline + index * 21.72, font_size,
                        )
                    continue
            _insert_student_value(
                page, fitted_value(field_name, width), x, baseline, font_size
            )
        for field_name, rect in placements.items():
            _insert_pdf_value(
                page, fitted_value(field_name, rect[2] - rect[0]), rect, font_size
            )

        if photo_bytes:
            _insert_admission_photo(page, photo_bytes)

        checkbox_centers = [
            (56.7, 172.4),
            (312.4, 172.4),
            (56.7, 204.9),
            (312.4, 204.9),
            (56.7, 237.4),
            (312.4, 237.4),
            (56.7, 256.8),
            (312.4, 256.8),
        ]
        for checked, (center_x, center_y) in zip(
            submitted_documents, checkbox_centers
        ):
            if checked:
                page.draw_line(
                    (center_x - 2.3, center_y - 2.3),
                    (center_x + 2.3, center_y + 2.3),
                    width=1.2,
                )
                page.draw_line(
                    (center_x + 2.3, center_y - 2.3),
                    (center_x - 2.3, center_y + 2.3),
                    width=1.2,
                )

        signature_page = document[1]
        for field_name, rect in signature_placements.items():
            _insert_pdf_value(
                signature_page, fitted_value(field_name, rect[2] - rect[0]), rect, font_size,
                cover_underline=True,
            )

        if annex:
            annex_page = document.new_page(width=612, height=792)
            y = 55
            for field, (number, value) in annex.items():
                lines = _wrap_pdf_value(
                    f"Anexo {number} - {PERSONAL_FIELDS[field]}: {value}", 510
                )
                for line in lines:
                    if y > 742:
                        annex_page = document.new_page(width=612, height=792)
                        y = 55
                    _insert_student_value(annex_page, line, 50, y, font_size)
                    y += 15
                y += 10

        return document.tobytes(garbage=3, deflate=True)


def _render_location_selector(
    label: str, key: str, locations: list[str], accept_new_options: bool = False
) -> str:
    options = ["", *locations]
    current_value = st.session_state.get(key, "")
    if current_value and current_value not in options:
        options.append(current_value)
    return st.selectbox(
        label, options, key=key, accept_new_options=accept_new_options,
    )


def _render_parent_address(
    form_key: str, field: str, label: str, student_address: str
) -> str:
    source = st.selectbox(
        label,
        ["Otra dirección", "La misma de la residente"],
        key=f"{form_key}_{field}_source",
    )
    same_address = source == "La misma de la residente"
    widget_key = f"{form_key}_{field}"
    if same_address:
        st.session_state[widget_key] = student_address
    return st.text_input(
        f"{label} - detalle", key=widget_key, disabled=same_address
    )


def _render_admission_form(tab_index: int, store: ResidentStore) -> None:
    form_key = f"admission_form_{tab_index}"
    draft_key = f"admission_draft_{tab_index}"
    values = {}

    for field, value in st.session_state.get(draft_key, {}).items():
        if field == "age":
            continue
        widget_key = f"{form_key}_{field}"
        if widget_key not in st.session_state:
            if field in ("birth_date", "admission_date"):
                value = datetime.strptime(value, "%d/%m/%Y").date() if value else None
            st.session_state[widget_key] = value
    for field in ("birth_date", "admission_date"):
        widget_key = f"{form_key}_{field}"
        if isinstance(st.session_state.get(widget_key), str):
            value = st.session_state[widget_key]
            st.session_state[widget_key] = (
                datetime.strptime(value, "%d/%m/%Y").date() if value else None
            )
    for field in PERSONAL_FIELDS:
        widget_key = f"{form_key}_{field}"
        value = st.session_state.get(widget_key)
        if isinstance(value, str) and field not in ("birth_date", "admission_date"):
            st.session_state[widget_key] = normalize_admission_values({field: value})[field]

    st.subheader("Planilla de admisión e ingreso")
    st.caption(
        "Al guardar el ingreso se registrarán los datos y la planilla en Residentes "
        "Activas. La primera mensualidad vence el día del ingreso; las siguientes, "
        "cada 30 días. "
        "La plantilla y las firmas originales no se modifican."
    )

    with st.container():
        st.markdown("#### Información de la estudiante")
        student_columns = st.columns(2)
        values["student_name"] = student_columns[0].text_input(
            "Nombres y apellidos *", key=f"{form_key}_student_name"
        )
        values["student_id"] = student_columns[1].text_input(
            "Cédula de identidad *", key=f"{form_key}_student_id"
        )

        student_columns = st.columns(3)
        birth_date = student_columns[0].date_input(
            "Fecha de nacimiento *", value=None,
            min_value=date(1900, 1, 1), max_value=date.today(),
            format="DD/MM/YYYY", key=f"{form_key}_birth_date"
        )
        values["birth_date"] = birth_date.strftime("%d/%m/%Y") if birth_date else ""
        age = calculate_age(birth_date, date.today()) if birth_date else ""
        st.session_state[f"{form_key}_age"] = age
        values["age"] = student_columns[1].text_input(
            "Edad (calculada)", disabled=True, key=f"{form_key}_age"
        )
        values["student_mobile"] = student_columns[2].text_input(
            "Teléfono celular *", key=f"{form_key}_student_mobile"
        )

        student_columns = st.columns(2)
        values["student_landline"] = student_columns[0].text_input(
            "Teléfono fijo", key=f"{form_key}_student_landline"
        )
        values["emergency_phone"] = student_columns[1].text_input(
            "Teléfono de emergencia *", key=f"{form_key}_emergency_phone"
        )

        values["email"] = st.text_input("Correo electrónico *", key=f"{form_key}_email")
        values["student_address"] = st.text_area(
            "Dirección de habitación actual *", key=f"{form_key}_student_address"
        )
        student_columns = st.columns(2)
        with student_columns[0]:
            values["city"] = _render_location_selector(
                "Ciudad *", f"{form_key}_city", VENEZUELAN_CITIES,
                accept_new_options=True,
            )
        with student_columns[1]:
            values["state"] = _render_location_selector(
                "Estado *", f"{form_key}_state", VENEZUELAN_STATES,
            )

        st.markdown("#### Información de los padres o representante legal")
        parent_columns = st.columns(2)
        values["mother_name"] = parent_columns[0].text_input(
            "Nombre de la madre / tutora", key=f"{form_key}_mother_name"
        )
        values["mother_phone"] = parent_columns[1].text_input(
            "Teléfono celular / trabajo (madre o tutora)",
            key=f"{form_key}_mother_phone",
        )
        parent_columns = st.columns(2)
        values["mother_workplace"] = parent_columns[0].text_input(
            "Lugar de trabajo (madre o tutora)",
            key=f"{form_key}_mother_workplace",
        )
        values["mother_landline"] = parent_columns[1].text_input(
            "Teléfono fijo / casa (madre o tutora)",
            key=f"{form_key}_mother_landline",
        )
        values["mother_address"] = _render_parent_address(
            form_key, "mother_address", "Dirección actual (madre o tutora)",
            values["student_address"],
        )

        parent_columns = st.columns(2)
        values["father_name"] = parent_columns[0].text_input(
            "Nombre del padre / tutor", key=f"{form_key}_father_name"
        )
        values["father_phone"] = parent_columns[1].text_input(
            "Teléfono celular / trabajo (padre o tutor)",
            key=f"{form_key}_father_phone",
        )
        parent_columns = st.columns(2)
        values["father_workplace"] = parent_columns[0].text_input(
            "Lugar de trabajo (padre o tutor)",
            key=f"{form_key}_father_workplace",
        )
        values["father_landline"] = parent_columns[1].text_input(
            "Teléfono fijo / casa (padre o tutor)",
            key=f"{form_key}_father_landline",
        )
        values["father_address"] = _render_parent_address(
            form_key, "father_address", "Dirección actual (padre o tutor)",
            values["student_address"],
        )
        values["emergency_contacts"] = st.text_input(
            "Teléfono y contactos de emergencia (vecino, abuelo, tío)",
            key=f"{form_key}_emergency_contacts",
        )
        values["representative_id"] = st.text_input(
            "Cédula de la madre", key=f"{form_key}_representative_id"
        )

        st.markdown("#### Recaudos consignados")
        document_columns = st.columns(2)
        submitted_documents = [
            document_columns[index % 2].checkbox(
                label, key=f"{form_key}_document_{index}"
            )
            for index, label in enumerate(DOCUMENT_LABELS)
        ]

        st.markdown("#### Datos administrativos")
        admin_columns = st.columns(3)
        admission_date = admin_columns[0].date_input(
            "Fecha de ingreso *", value=None,
            min_value=date(1900, 1, 1), max_value=date(2100, 12, 31),
            format="DD/MM/YYYY", key=f"{form_key}_admission_date"
        )
        values["admission_date"] = (
            admission_date.strftime("%d/%m/%Y") if admission_date else ""
        )
        values["room_number"] = admin_columns[1].text_input(
            "Habitación N.º *", key=f"{form_key}_room_number"
        )
        values["bed_number"] = admin_columns[2].text_input(
            "Cama N.º *", key=f"{form_key}_bed_number"
        )
        photo = st.file_uploader(
            "Foto tipo carnet (opcional)",
            type=["jpg", "jpeg", "png"],
            key=f"{form_key}_photo",
            help="La foto llena el recuadro completo, sin deformarse. "
            "Se recortan los bordes de forma centrada si es necesario.",
        )

        accepted_commitments = st.checkbox(
            "Confirmo que leí las normas de convivencia. La planilla descargada "
            "deberá imprimirse para firmarla.",
            key=f"{form_key}_accepted_commitments",
        )
        submitted = st.button(
            "Guardar ingreso y generar planilla", type="primary", key=f"{form_key}_save"
        )

    values = normalize_admission_values(values)
    st.session_state[draft_key] = {
        **values,
        **{
            f"{field}_source": st.session_state[f"{form_key}_{field}_source"]
            for field in ("mother_address", "father_address")
        },
        **{
            f"document_{index}": checked
            for index, checked in enumerate(submitted_documents)
        },
        "accepted_commitments": accepted_commitments,
    }
    if submitted:
        required_fields = {
            "Nombres y apellidos": values["student_name"],
            "Cédula de identidad": values["student_id"],
            "Fecha de nacimiento": values["birth_date"],
            "Edad": values["age"],
            "Teléfono celular": values["student_mobile"],
            "Teléfono de emergencia": values["emergency_phone"],
            "Correo electrónico": values["email"],
            "Dirección actual": values["student_address"],
            "Ciudad": values["city"],
            "Estado": values["state"],
            "Fecha de ingreso": values["admission_date"],
            "Habitación": values["room_number"],
            "Cama": values["bed_number"],
        }
        missing_fields = [
            label for label, value in required_fields.items() if not value.strip()
        ]
        if not values["mother_name"].strip() and not values["father_name"].strip():
            missing_fields.append("Nombre de al menos un padre, madre o tutor")

        if missing_fields:
            st.error("Completa estos campos obligatorios: " + ", ".join(missing_fields))
        elif not accepted_commitments:
            st.error("Confirma que leíste las normas de convivencia para continuar.")
        elif not ADMISSION_TEMPLATE.is_file():
            st.error(
                "No se encontró la planilla PDF original. Comprueba que el archivo "
                "Planilla_de_Ingreso_Residencia_Carmen_Luisa.pdf esté junto a app.py."
            )
        else:
            try:
                admission = datetime.strptime(
                    values["admission_date"].strip(), "%d/%m/%Y"
                ).date()
            except ValueError:
                st.error("La fecha de ingreso debe ser válida y tener formato DD/MM/AAAA.")
                return
            values["admission_date"] = admission.strftime("%d/%m/%Y")
            try:
                pdf = _generate_admission_pdf(
                    values,
                    submitted_documents,
                    photo.getvalue() if photo else None,
                )
            except (pymupdf.FileDataError, ValueError) as error:
                LOGGER.exception("Error al generar una planilla de ingreso")
                st.error(
                    "No se pudo generar el PDF. Revisa la foto adjunta y que cada "
                    f"dato quepa en su espacio de la planilla. {error}"
                )
            else:
                try:
                    store.add(
                        tab_index, values, submitted_documents, pdf,
                        photo.getvalue() if photo else None,
                    )
                except ValueError as error:
                    st.error(str(error))
                except (sqlite3.Error, OSError):
                    LOGGER.exception("Error al guardar un ingreso")
                    st.error(
                        "No se pudo guardar el ingreso. Los campos siguen aquí; "
                        "no se ha confirmado el registro. Intenta guardarlo de nuevo."
                    )
                else:
                    st.session_state[f"residents_notice_{tab_index}"] = (
                        "Ingreso guardado. Descarga la planilla completada desde "
                        "el registro de la residente en Residentes Activas."
                    )
                    st.session_state[f"show_inactive_{tab_index}"] = False
                    st.session_state[f"resident_search_{tab_index}_False_False"] = ""
                    st.session_state[f"residence_view_{tab_index}"] = 1
                    st.rerun()

    if st.button("Cerrar formulario", key=f"{form_key}_close"):
        st.session_state[f"residence_view_{tab_index}"] = None
        st.rerun()


def _render_monthly_received(tab_index: int, store: ResidentStore) -> None:
    today = date.today()
    start = today.replace(day=1)
    st.subheader("Pagos recibidos en el mes")
    notice = st.session_state.pop(f"invoice_notice_{tab_index}", None)
    if notice:
        st.success(notice)
    st.caption(
        f"Mes actual: {today:%m/%Y}. Mensualidades, depósitos y servicios según la fecha del pago, "
        "incluidas residentes que ingresaron antes o que ahora están de baja. "
        "Los acumulados incluyen saldos iniciales y cobros recibidos, no saldo disponible: "
        "no descuentan gastos ni devoluciones."
    )
    previous = {"VES": Decimal("0"), "USD": Decimal("0")}
    current = {"VES": Decimal("0"), "USD": Decimal("0")}
    bank_totals = {
        bank: dict.fromkeys(("previous", "current", "opening", "opening_previous"), Decimal("0"))
        for bank in BANKS
    }
    dollar_totals = {
        method: dict.fromkeys(("previous", "current", "opening", "opening_previous"), Decimal("0"))
        for method in ("Efectivo", "Zelle", "Binance")
    }
    incomplete = 0
    monthly_vat = Decimal("0")
    entries = []
    month_payments = []
    for payment in store.received_payments(tab_index, today):
        currency = payment["currency"]
        raw_amount = payment["amount"]
        valid = currency in current and raw_amount is not None
        amount = None
        if raw_amount is not None and currency in current:
            try:
                amount = Decimal(raw_amount)
                valid = amount.is_finite() and amount > 0
            except InvalidOperation:
                valid = False
        if not valid:
            incomplete += 1
        in_month = payment["paid_on"] >= start.isoformat()
        if valid and amount is not None and currency is not None:
            totals = current if in_month else previous
            totals[currency] += amount
            grouped_totals = bank_totals if currency == "VES" else dollar_totals
            method = (
                payment.get("receiving_bank") if currency == "VES"
                else payment.get("payment_method")
            ) or "No registrado"
            if method not in grouped_totals:
                grouped_totals[method] = dict.fromkeys(
                    ("previous", "current", "opening", "opening_previous"), Decimal("0"),
                )
            grouped_totals[method]["current" if in_month else "previous"] += amount
        if in_month:
            month_payments.append(payment)
            vat = (
                calculate_receipt_vat(amount)
                if valid and amount is not None and currency == "VES"
                and (
                    payment["kind"] == "Mensualidad"
                    or payment["kind"] in {f"Otros servicios: {concept}" for concept in SERVICE_CONCEPTS}
                ) else None
            )
            if vat is not None:
                monthly_vat += vat
            rate = payment.get("exchange_rate") if currency == "VES" else None
            rate_date = payment.get("exchange_rate_date")
            try:
                parsed_rate = Decimal(rate) if rate else None
                formatted_rate = (
                    f"{parsed_rate:,.4f}".translate(str.maketrans(",.", ".,"))
                    if parsed_rate is not None and parsed_rate.is_finite() and parsed_rate > 0
                    else None
                )
            except InvalidOperation:
                formatted_rate = None
            entries.append({
                "Fecha": date.fromisoformat(payment["paid_on"]).strftime("%d/%m/%Y"),
                "Residente": payment["student_name"], "Cédula de identidad": payment["student_id"],
                "Habitación": payment.get("room_number") or "Sin indicar",
                "Cama": payment.get("bed_number") or "Sin indicar", "Tipo de pago": payment["kind"],
                "N.º de factura": payment.get("invoice_number") or "Sin indicar",
                "Bolívares (Bs.)": format_bolivars(amount)
                if valid and amount is not None and currency == "VES" else "—",
                "IVA (16%) (Bs.)": (
                    format_bolivars(vat) if vat is not None else "—"
                ),
                "Tasa BCV (Bs./USD)": formatted_rate or (
                    "No registrado" if currency == "VES" else "—"
                ),
                "Fecha de la tasa": (
                    date.fromisoformat(rate_date).strftime("%d/%m/%Y")
                    if rate_date else
                    "No registrada" if currency == "VES" else "—"
                ),
                "Banco receptor": payment.get("receiving_bank") or (
                    "No registrado" if currency == "VES" else "—"
                ),
                "Dólares (USD)": f"{amount:.2f}".replace(".", ",")
                if valid and currency == "USD" else "—",
                "Forma de pago USD": payment.get("payment_method") or (
                    "No registrado" if currency == "USD" else "—"
                ),
            })
    collection_rows = entries.copy()
    if entries:
        collection_rows.append({
            **dict.fromkeys(entries[0], ""),
            "Residente": "TOTAL",
            "Bolívares (Bs.)": format_bolivars(current["VES"]),
            "IVA (16%) (Bs.)": format_bolivars(monthly_vat),
            "Dólares (USD)": f"{current['USD']:.2f}".replace(".", ","),
        })
        st.markdown("**Resumen de cobros**")
        st.table(collection_rows)
        with st.expander("Ingresar o corregir números de factura"):
            st.caption(
                "Guarda el número de la factura entregada para cada pago. "
                "Es independiente del número automático del recibo. Deja vacío para quitarlo."
            )
            for payment, entry in zip(month_payments, entries):
                record_id = payment.get("record_id")
                if record_id is None:
                    st.warning(
                        "Un pago no tiene identificador para editar su factura. "
                        "Los cobros y totales siguen disponibles. Reinicia la "
                        "aplicación para cargar la versión actual del registro."
                    )
                    continue
                invoice_key = f"invoice_{tab_index}_{payment['kind']}_{payment['record_id']}"
                with st.form(invoice_key):
                    displayed_amount = (
                        entry["Bolívares (Bs.)"] if payment["currency"] == "VES"
                        else entry["Dólares (USD)"]
                    )
                    st.write(
                        f"{payment['student_name']} — {payment['student_id']} — "
                        f"{payment['kind']} — {payment['paid_on']} — "
                        f"{payment['currency'] or 'Moneda no registrada'} "
                        f"{displayed_amount if displayed_amount != '—' else 'Monto no registrado'}"
                    )
                    invoice_number = st.text_input(
                        "Número de factura entregada",
                        value=payment.get("invoice_number") or "", max_chars=40,
                        key=f"{invoice_key}_number",
                    )
                    save_invoice = st.form_submit_button("Guardar número de factura")
                if save_invoice:
                    try:
                        store.set_invoice_number(
                            tab_index, payment["kind"], payment["record_id"], invoice_number,
                        )
                    except ValueError as error:
                        st.error(str(error))
                    except (sqlite3.Error, OSError):
                        LOGGER.exception("Error al guardar el número de factura")
                        st.error("No se pudo guardar el número de factura. Intenta de nuevo.")
                    else:
                        st.session_state[f"invoice_notice_{tab_index}"] = "Número de factura guardado."
                        st.rerun()
    else:
        st.info("No hay pagos recibidos en este mes.")
    warning = ""
    if incomplete:
        warning = (
            f"{incomplete} pago(s) anterior(es) con monto o moneda incompletos no se sumaron. "
            "Los totales pueden estar incompletos; no se inventaron importes."
        )
        st.warning(warning)
    opening = {"VES": Decimal("0"), "USD": Decimal("0")}
    opening_previous = {"VES": Decimal("0"), "USD": Decimal("0")}
    opening_rows = []
    for balance in store.opening_balances(tab_index, today):
        balance_amount = Decimal(balance["amount"])
        opening[balance["currency"]] += balance_amount
        if balance["as_of"] < start.isoformat():
            opening_previous[balance["currency"]] += balance_amount
        grouped_totals = bank_totals if balance["currency"] == "VES" else dollar_totals
        method = balance["payment_method"]
        grouped_totals[method]["opening"] += balance_amount
        if balance["as_of"] < start.isoformat():
            grouped_totals[method]["opening_previous"] += balance_amount
        opening_rows.append({
            "Resumen de cobros": (
                f"Saldo inicial — {balance['payment_method']} — "
                f"{date.fromisoformat(balance['as_of']):%d/%m/%Y}: {balance['description']}"
            ),
            "Bolívares (Bs.)": format_bolivars(balance_amount)
            if balance["currency"] == "VES" else "0,00",
            "Dólares (USD)": f"{balance_amount:.2f}".replace(".", ",")
            if balance["currency"] == "USD" else "0,00",
        })
    summary = opening_rows + [
        {
            "Resumen de cobros": label,
            "Bolívares (Bs.)": format_bolivars(amounts["VES"]),
            "Dólares (USD)": f"{amounts['USD']:.2f}".replace(".", ","),
        }
        for label, amounts in (
            ("Acumulado de meses anteriores", {
                currency: previous[currency] + opening_previous[currency] for currency in current
            }),
            ("Total recibido en el mes", current),
            ("Acumulado al cierre del mes", {
                currency: previous[currency] + current[currency] + opening[currency]
                for currency in current
            }),
        )
    ]
    st.markdown("**Totales y acumulados**")
    st.table(summary)
    def grouped_summary(
        groups: dict[str, dict[str, Decimal]], group_label: str, unit: str,
    ) -> list[dict[str, str]]:
        rows = []
        for method, totals in groups.items():
            amounts = {
                f"Saldo inicial / acumulado anterior ({unit})": totals["previous"] + totals["opening"],
                f"Recibido en el mes ({unit})": totals["current"],
                f"Acumulado al cierre ({unit})": totals["previous"] + totals["current"] + totals["opening"],
            }
            rows.append({
                group_label: method,
                **{
                    label: format_bolivars(value) if unit == "Bs."
                    else f"{value:.2f}".replace(".", ",")
                    for label, value in amounts.items()
                },
            })
        return rows

    bank_summary = grouped_summary(bank_totals, "Banco receptor", "Bs.")
    dollar_summary = grouped_summary(dollar_totals, "Forma de pago USD", "USD")
    st.markdown("**Resumen de Bolivares en Bancos**")
    st.table(bank_summary)
    st.caption(
        "Cada banco muestra saldos iniciales y cobros acumulados, no saldo bancario disponible. "
        "Saldo inicial / acumulado anterior reúne los cobros anteriores y todos los "
        "saldos de arranque registrados hasta este mes, una sola vez. "
        "Los pagos sin banco identificado se muestran como «No registrado»."
    )
    st.markdown("**Resumen de dólares Recibidos**")
    st.table(dollar_summary)
    st.caption(
        "Solo incluye dólares realmente recibidos y saldos iniciales en USD. "
        "Saldo inicial / acumulado anterior reúne los cobros anteriores y todos los "
        "saldos de arranque registrados hasta este mes, una sola vez. "
        "No incluye la base en dólares de pagos recibidos en bolívares ni descuenta devoluciones."
    )
    try:
        pdf = generate_monthly_report(
            tab_index, today, collection_rows, summary, warning,
            bank_summary=bank_summary, dollar_summary=dollar_summary,
        )
    except (ValueError, OSError, pymupdf.FileDataError):
        LOGGER.exception("Error al generar la lista mensual de pagos")
        st.error("No se pudo generar el PDF de la lista mensual. Los pagos guardados no cambiaron.")
    else:
        st.download_button(
            "Descargar lista de pagos del mes (PDF)",
            data=pdf,
            file_name=f"Pagos_Recibidos_{tab_index}_{today:%Y_%m}.pdf",
            mime="application/pdf", key=f"monthly_received_pdf_{tab_index}",
        )


def _render_service_payment(
    store: ResidentStore, resident_id: int, residence: int, key: str, today: date,
) -> None:
    st.markdown("**Pago por otros Servicios**")
    with st.popover("Recibir o confirmar pago por otros servicios"):
        concept = st.selectbox("Concepto del servicio", SERVICE_CONCEPTS, key=f"{key}_service_concept")
        currency = st.selectbox(
            "Moneda del pago del servicio",
            ["Selecciona una moneda", "Bolívares", "Dólares"], key=f"{key}_service_currency",
        )
        amount_input = st.text_input(
            "Monto del servicio en dólares (USD)", placeholder="Ejemplo: 20,00",
            key=f"{key}_service_amount",
        ).strip()
        amount = None
        error_message = None
        if amount_input:
            try:
                amount = Decimal(amount_input.replace(",", "."))
                if (
                    not amount.is_finite() or amount <= 0
                    or amount != amount.quantize(Decimal("0.01"))
                ):
                    error_message = "El monto debe ser positivo con máximo dos decimales."
            except InvalidOperation:
                error_message = "Ingresa un monto de servicio válido."
        payment_method = None
        rate = None
        rate_date = None
        bank = None
        received = amount
        if currency == "Bolívares":
            selected_bank = st.selectbox(
                "Banco que recibió los bolívares",
                ["Selecciona el banco receptor", *BANKS], key=f"{key}_service_bank",
            )
            if selected_bank in BANKS:
                bank = selected_bank
            rate_input = st.text_input(
                "Tasa del día BCV (Bs. por USD)", key=f"{key}_service_rate",
                placeholder="Ejemplo: 881,0664",
            ).strip()
            rate_date = st.date_input(
                "Fecha de la tasa BCV", value=today, format="DD/MM/YYYY",
                key=f"{key}_service_rate_date",
            )
            if rate_input:
                try:
                    rate = Decimal(rate_input.replace(",", "."))
                    if amount is not None and error_message is None:
                        received = bolivars_from_dollars(amount, rate)
                        vat = calculate_receipt_vat(received)
                        st.info(
                            f"Subtotal: Bs. {format_bolivars(received - vat)}. "
                            f"IVA (16%): Bs. {format_bolivars(vat)}. "
                            f"Total a recibir: Bs. {format_bolivars(received)}."
                        )
                except (InvalidOperation, ValueError) as error:
                    error_message = (
                        str(error) if isinstance(error, ValueError) else "Ingresa una tasa BCV válida."
                    )
        elif currency == "Dólares":
            selected_method = st.selectbox(
                "Forma de pago en dólares", ["Selecciona una forma de pago", "Efectivo", "Binance", "Zelle"],
                key=f"{key}_service_method",
            )
            if selected_method != "Selecciona una forma de pago":
                payment_method = selected_method
            st.caption("Los servicios recibidos en dólares no llevan IVA.")
        if error_message:
            st.error(error_message)
        with st.form(f"{key}_service"):
            invoice_number = st.text_input(
                "Número de factura", max_chars=40, key=f"{key}_service_invoice",
                help="Se guarda con el servicio y se imprime en ambas copias del recibo.",
            )
            confirmed = st.checkbox(
                "Confirmo que recibí este pago de servicio", key=f"{key}_service_confirmed",
            )
            submit = st.form_submit_button("Registrar pago de servicio")
        if submit:
            if not confirmed:
                st.error("Confirma la recepción del pago de servicio.")
            elif error_message:
                st.error(error_message)
            elif received is None:
                st.error("Ingresa un monto de servicio válido.")
            else:
                try:
                    store.record_service_payment(
                        resident_id, residence, concept, today,
                        {"Bolívares": "VES", "Dólares": "USD"}.get(currency, ""),
                        received, payment_method=payment_method,
                        exchange_rate=rate, exchange_rate_date=rate_date,
                        amount_usd=amount if currency == "Bolívares" else None,
                        receiving_bank=bank, invoice_number=invoice_number,
                    )
                except ValueError as error:
                    st.error(str(error))
                except (OSError, pymupdf.FileDataError, sqlite3.Error):
                    LOGGER.exception("Error al guardar el servicio y su recibo")
                    st.error("No se pudo generar o guardar el recibo. El pago de servicio no se guardó.")
                else:
                    st.session_state[f"residents_notice_{residence}"] = (
                        "Pago de servicio guardado. Recibo disponible en el historial. "
                        "El vencimiento de la mensualidad no cambió."
                    )
                    st.rerun()


def _render_residents(
    tab_index: int, store: ResidentStore, pending_only: bool = False,
    inactive: bool = False,
) -> None:
    st.subheader(
        "Residentes de Baja" if inactive else
        "Pagos Pendientes" if pending_only else "Residentes Activas"
    )
    notice = st.session_state.pop(f"residents_notice_{tab_index}", None)
    if notice:
        st.success(notice)
    latest_payment = st.session_state.get(f"latest_monthly_payment_{tab_index}")
    if pending_only and latest_payment is not None:
        resident_id, paid_due, student_name = latest_payment
        receipt = store.get_payment_receipt(resident_id, tab_index, paid_due)
        if receipt is not None:
            st.caption(f"Última mensualidad registrada: {student_name}.")
            st.download_button(
                "Descargar recibo de pago de mensualidad (PDF)",
                data=receipt,
                file_name=(
                    f"Recibo_Mensualidad_{store.receipt_number(resident_id, tab_index, paid_due) or paid_due.isoformat()}.pdf"
                ),
                mime="application/pdf", key=f"latest_monthly_receipt_{tab_index}",
            )
        else:
            st.error("No se encontró el recibo del último pago. Consulta el historial de la residente.")
    if inactive:
        st.caption("Consulta los datos, planillas, pagos y recibos de las residentes dadas de baja.")
    else:
        st.caption(
            "Primera mensualidad el día del ingreso; siguientes cada 30 días. "
            "Aviso desde 3 días antes. "
            "Cada pago registrado cubre un período de 30 días, sin mover el calendario "
            "original. Los recordatorios por correo requieren configurar Gmail "
            "y mantener activo el proceso de envíos."
        )
    if pending_only:
        try:
            MailConfig.from_environment()
        except ValueError as error:
            st.info(f"Recordatorios por correo pendientes de configuración. {error}")
        else:
            st.caption(
                "Correo configurado: el proceso de recordatorios debe estar activo "
                "para enviar desde las 08:00, hora de Venezuela. Configurar Gmail "
                "no inicia el proceso automáticamente."
            )
        reminder_counts = store.reminder_counts(
            tab_index, datetime.now(VENEZUELA).date(),
        )
        st.caption(
            f"Correos de hoy aceptados por Gmail: {reminder_counts.get('sent', 0)}."
        )
        unresolved = sum(
            reminder_counts.get(status, 0) for status in ("failed", "unknown", "sending")
        )
        if unresolved:
            st.warning(
                f"{unresolved} recordatorio(s) no enviado(s) o sin confirmación hoy. "
                "Revisa el registro del proceso y Gmail. No se reintentan "
                "automáticamente el mismo día para evitar duplicados."
            )
    if not pending_only and not inactive:
        inactive = st.checkbox(
            "Ver historial de bajas", key=f"show_inactive_{tab_index}"
        )
    residents = store.list_residents(tab_index, inactive=inactive)
    today = date.today()
    alerted = [
        resident for resident in residents
        if (resident.next_payment - today).days <= ALERT_DAYS
    ]
    if pending_only:
        residents = alerted
        st.caption(
            "Se incluyen desde 3 días antes del vencimiento y permanecen aquí mientras "
            "el próximo pago esté pendiente dentro de ese plazo. También siguen en Residentes Activas."
        )
    elif alerted and not inactive:
        st.warning(
            f"{len(alerted)} residente(s) con cobro vencido, para hoy o en los próximos "
            "3 días. Revisa las fechas indicadas abajo."
        )
    search = st.text_input(
        "Buscar por nombre o cédula",
        key=f"resident_search_{tab_index}_{pending_only}_{inactive}",
    ).strip().casefold()
    if search:
        residents = [
            resident for resident in residents
            if search in resident.values["student_name"].casefold()
            or search in resident.values["student_id"].casefold()
        ]
    if not residents:
        st.info("No hay residentes que mostrar en esta vista.")
        return

    for resident in residents:
        key = f"resident_{tab_index}_{resident.id}_{pending_only}_{inactive}"
        label = f"{resident.values['student_name']} — {resident.values['student_id']}"
        if not pending_only and not inactive and (
            resident.next_payment - today
        ).days <= ALERT_DAYS:
            label = label.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
            label = (
                f":red[{label} — {payment_status(resident.next_payment, today)} "
                f"({resident.next_payment:%d/%m/%Y})]"
            )
        with st.expander(
            label,
            expanded=(
                inactive and resident.id
                == st.session_state.get(f"latest_discharged_resident_{tab_index}")
            ),
        ):
            refund = (
                store.deposit_refund(resident.id, tab_index)
                if resident.discharged_on is not None else None
            )
            if refund is not None:
                st.markdown("**CONSTANCIA DE ENTREGA DE DEPÓSITO**")
                st.info(
                    f"Constancia N.º {refund['receipt_number']} — "
                    f"Depósito: USD {Decimal(refund['deposit_amount']):.2f} — "
                    f"Descuento: USD {Decimal(refund['deduction']):.2f} — "
                    f"Reembolso: USD {Decimal(refund['amount']):.2f}."
                )
                st.download_button(
                    "**Descargar constancia de entrega de depósito (PDF)**",
                    data=refund["receipt_pdf"],
                    file_name=f"Constancia_Entrega_Deposito_{refund['receipt_number']}.pdf",
                    mime="application/pdf", key=f"{key}_refund_receipt",
                )
            st.table([
                {"Dato": label, "Valor": resident.values.get(field) or "Sin indicar"}
                for field, label in PERSONAL_FIELDS.items()
            ])
            st.markdown("**Recaudos consignados**")
            st.table([
                {"Recaudo": label, "Consignado": "Sí" if checked else "No"}
                for label, checked in zip(DOCUMENT_LABELS, resident.documents)
            ])
            st.download_button(
                "Descargar planilla completada (PDF)",
                data=store.get_pdf(resident.id, tab_index),
                file_name=f"Planilla_Ingreso_{resident.id}.pdf",
                mime="application/pdf", key=f"{key}_download",
            )
            if store.can_delete_admission(resident.id, tab_index):
                with st.form(f"{key}_delete_admission"):
                    st.warning(
                        "Eliminar el ingreso borra definitivamente el registro, "
                        "los datos, la foto y la planilla PDF. Solo se permite "
                        "si no hay depósito ni mensualidades registradas."
                    )
                    confirmed = st.checkbox(
                        "Confirmo que deseo eliminar el ingreso completo",
                        key=f"{key}_delete_confirmed",
                    )
                    delete = st.form_submit_button("Eliminar planilla de ingreso")
                if delete:
                    if not confirmed:
                        st.error("Confirma que deseas eliminar el ingreso completo.")
                    else:
                        try:
                            store.delete_admission(resident.id, tab_index)
                        except ValueError as error:
                            st.error(str(error))
                        except (sqlite3.Error, OSError):
                            LOGGER.exception("Error al eliminar el ingreso")
                            st.error(
                                "No se pudo eliminar el ingreso. Vuelve a consultar "
                                "el registro antes de intentarlo de nuevo."
                            )
                        else:
                            st.session_state[f"residents_notice_{tab_index}"] = (
                                "Ingreso eliminado. Se borraron los datos, la foto "
                                "y la planilla PDF."
                            )
                            st.rerun()
            deposit = store.deposit_details(resident.id, tab_index)
            st.markdown("**Pago Recibido Deposito**")
            st.caption(
                "Cobro único de depósito por ingreso. Este monto le será reembolsado "
                "al retirarse de la residencia, en el mismo tipo de moneda recibido. "
                "Algunas condiciones aplican."
            )
            if deposit is not None:
                deposit_date = date.fromisoformat(deposit["paid_on"])
                st.info(f"Fecha del pago del depósito: {deposit_date:%d/%m/%Y}.")
                st.write(
                    "Moneda del depósito: "
                    + ("Dólares (USD)" if deposit["currency"] == "USD" else "No registrado")
                )
                st.write(
                    "Monto recibido del depósito: "
                    + (
                        f"USD {deposit['amount'].replace('.', ',')}"
                        if deposit["amount"] else "No registrado"
                    )
                )
                st.write(
                    f"Forma de pago del depósito: {deposit['payment_method'] or 'No registrado'}"
                )
            elif resident.discharged_on is None:
                deposit_panel = (
                    st.container() if pending_only else
                    st.popover("Recibir o confirmar depósito")
                )
                with deposit_panel, st.form(f"{key}_deposit"):
                    st.caption("Pago recibido en Dólares (USD).")
                    deposit_amount_input = st.text_input(
                        "Monto recibido del depósito (USD)",
                        placeholder="Ejemplo: 150,00",
                        key=f"{key}_deposit_amount",
                    ).strip()
                    deposit_method = st.selectbox(
                        "Forma de pago del depósito en dólares",
                        ["Selecciona una forma de pago", "Efectivo", "Binance", "Zelle"],
                        key=f"{key}_deposit_method",
                    )
                    paid_on = st.date_input(
                        "Fecha del pago del depósito",
                        value=today,
                        max_value=today,
                        format="DD/MM/YYYY",
                    )
                    deposit_confirmed = st.checkbox("Confirmo que recibí el depósito")
                    save_deposit = st.form_submit_button("Guardar depósito")
                if save_deposit:
                    if not deposit_confirmed:
                        st.error("Confirma la recepción del depósito para guardarlo.")
                    else:
                        try:
                            store.record_deposit(
                                resident.id, tab_index, paid_on, deposit_method,
                                Decimal(deposit_amount_input.replace(",", ".")),
                            )
                        except InvalidOperation:
                            st.error("Ingresa un monto del depósito válido mayor que cero.")
                        except ValueError as error:
                            st.error(str(error))
                        except (OSError, pymupdf.FileDataError, sqlite3.Error):
                            LOGGER.exception("Error al guardar el depósito y su recibo")
                            st.error(
                                "No se pudo generar o guardar el recibo PDF. "
                                "El depósito no se guardó; intenta de nuevo."
                            )
                        else:
                            st.session_state[f"residents_notice_{tab_index}"] = (
                                "Depósito único guardado. Recibo PDF disponible para descargar."
                            )
                            st.rerun()
            else:
                st.info("Depósito no registrado.")
            if resident.discharged_on is not None:
                st.info(
                    f"Baja: {resident.discharged_on:%d/%m/%Y}. "
                    f"Motivo: {resident.discharge_reason}"
                )
                if refund is not None:
                    st.markdown("**Entrega de depósito**")
                    st.write(
                        f"Depósito original: USD {refund['deposit_amount']}. "
                        f"Descuento: USD {refund['deduction']}. "
                        f"Total reembolsado: USD {refund['amount']}."
                    )
                    if refund["deduction_reason"]:
                        st.write(f"Descuento por: {refund['deduction_reason']}")
                    st.write(
                        f"Llaves entregadas: {'Sí' if refund['keys_returned'] else 'No'}. "
                        f"Revisión: {'SIN NOVEDAD' if refund['inspection_ok'] else 'Con novedades'}. "
                        f"Forma de devolución: {refund['payment_method']}."
                    )
                else:
                    st.info("No hay devolución de depósito registrada para esta baja.")
            else:
                due_text = (
                    f"Próximo cobro: {resident.next_payment:%d/%m/%Y} — "
                    f"{payment_status(resident.next_payment, today)}"
                )
                if resident.next_payment < today:
                    st.error(due_text)
                elif (resident.next_payment - today).days <= ALERT_DAYS:
                    st.warning(due_text)
                else:
                    st.info(due_text)
                payment_panel = (
                    st.container() if pending_only else
                    st.popover("Recibir o confirmar mensualidad")
                )
                with payment_panel:
                    st.markdown("**Pago recibido — Mensualidad**")
                    st.caption(
                        f"Registrar el pago correspondiente al vencimiento "
                        f"{resident.next_payment:%d/%m/%Y}."
                    )
                    currency = st.selectbox(
                        "Moneda del pago",
                        ["Selecciona una moneda", "Bolívares", "Dólares"],
                        key=f"{key}_payment_currency",
                    )
                    amount_input = ""
                    amount = None
                    monthly_amount_usd = None
                    late_fee_usd = Decimal("0")
                    late_days = max(0, (today - resident.next_payment).days)
                    late_fee_error = None
                    if currency != "Selecciona una moneda":
                        amount_input = st.text_input(
                            "Monto de mensualidad en dólares (USD)",
                            placeholder="Ejemplo: 150,00",
                            key=f"{key}_payment_amount",
                        ).strip()
                        if amount_input:
                            try:
                                parsed_amount = Decimal(amount_input.replace(",", "."))
                                if parsed_amount.is_finite() and parsed_amount > 0:
                                    amount = parsed_amount
                            except InvalidOperation:
                                pass
                        monthly_amount_usd = amount
                        if late_days:
                            st.warning(f"Días de retardo: {late_days}. La mora se ingresa manualmente.")
                            late_fee_input = st.text_input(
                                "Monto de mora (USD)", value="0",
                                key=f"{key}_payment_late_fee",
                            ).strip()
                            try:
                                late_fee_usd = Decimal(late_fee_input.replace(",", "."))
                                if (
                                    not late_fee_usd.is_finite() or late_fee_usd < 0
                                    or late_fee_usd != late_fee_usd.quantize(Decimal("0.01"))
                                ):
                                    late_fee_error = "La mora debe ser no negativa y tener máximo dos decimales."
                            except InvalidOperation:
                                late_fee_error = "Ingresa un monto de mora válido; usa cero si no hay recargo."
                            if late_fee_error:
                                st.error(late_fee_error)
                        if amount is not None and late_fee_error is None:
                            amount += late_fee_usd
                            st.info(
                                f"Mensualidad: USD {monthly_amount_usd:.2f}. "
                                f"Mora: USD {late_fee_usd:.2f}. "
                                f"Total a recibir: USD {amount:.2f}."
                            )
                    payment_method = None
                    exchange_rate = None
                    exchange_rate_date = None
                    amount_usd = None
                    conversion_error = None
                    receiving_bank = None
                    if currency == "Bolívares":
                        bank = st.selectbox(
                            "Banco que recibió los bolívares",
                            ["Selecciona el banco receptor", *BANKS],
                            key=f"{key}_payment_bank",
                        )
                        if bank in BANKS:
                            receiving_bank = bank
                        rate_input = st.text_input(
                            "Tasa del día BCV (Bs. por USD)",
                            placeholder="Ejemplo: 881,0664",
                            key=f"{key}_payment_exchange_rate",
                        ).strip()
                        rate_date = st.date_input(
                            "Fecha de la tasa BCV",
                            value=today,
                            format="DD/MM/YYYY",
                            key=f"{key}_payment_exchange_rate_date",
                        )
                        if rate_input:
                            try:
                                parsed_rate = Decimal(rate_input.replace(",", "."))
                                if parsed_rate.is_finite() and parsed_rate > 0:
                                    exchange_rate = parsed_rate
                                    exchange_rate_date = rate_date
                            except InvalidOperation:
                                pass
                        if amount is not None and exchange_rate is not None and late_fee_error is None:
                            amount_usd = amount
                            try:
                                amount = bolivars_from_dollars(amount_usd, exchange_rate)
                            except ValueError as error:
                                conversion_error = str(error)
                                st.error(conversion_error)
                            else:
                                st.info(
                                    "Bolívares a recibir: **Bs. "
                                    + f"{amount:.2f}".replace(".", ",")
                                    + "**"
                                )
                    elif currency == "Dólares":
                        selected_method = st.selectbox(
                            "Forma de pago en dólares",
                            ["Selecciona una forma de pago", "Efectivo", "Binance", "Zelle"],
                            key=f"{key}_payment_method",
                        )
                        if selected_method != "Selecciona una forma de pago":
                            payment_method = selected_method
                    with st.form(f"{key}_payment"):
                        invoice_number = st.text_input(
                            "Número de factura", max_chars=40,
                            key=f"{key}_payment_invoice_number",
                            help="Ingreso manual. Se guarda con el pago y se imprime "
                            "en ambas copias del recibo de mensualidad.",
                        )
                        confirmed = st.checkbox("Confirmo que recibí este pago")
                        paid = st.form_submit_button("Registrar pago")
                if paid:
                    if not confirmed:
                        st.error("Confirma la recepción del pago para registrarlo.")
                    elif currency == "Selecciona una moneda":
                        st.error("Selecciona la moneda en que se recibió el pago.")
                    elif late_fee_error:
                        st.error(late_fee_error)
                    elif amount is None:
                        st.error("Ingresa una cantidad recibida válida mayor que cero.")
                    elif currency == "Bolívares" and exchange_rate is None:
                        st.error("Ingresa una tasa BCV válida mayor que cero.")
                    elif currency == "Bolívares" and receiving_bank is None:
                        st.error("Selecciona el banco que recibió los bolívares.")
                    elif currency == "Dólares" and payment_method is None:
                        st.error("Selecciona efectivo, Binance o Zelle.")
                    elif conversion_error:
                        st.error(conversion_error)
                    else:
                        try:
                            store.record_payment(
                                resident.id, tab_index, resident.next_payment, today,
                                "VES" if currency == "Bolívares" else "USD",
                                amount=amount,
                                payment_method=payment_method,
                                exchange_rate=exchange_rate,
                                exchange_rate_date=exchange_rate_date,
                                amount_usd=amount_usd,
                                monthly_amount_usd=monthly_amount_usd,
                                late_fee_usd=late_fee_usd,
                                receiving_bank=receiving_bank,
                                invoice_number=invoice_number,
                            )
                        except ValueError as error:
                            st.error(str(error))
                        except (OSError, pymupdf.FileDataError, sqlite3.Error):
                            LOGGER.exception("Error al guardar la mensualidad y su recibo")
                            st.error(
                                "No se pudo generar o guardar el recibo de mensualidad. "
                                "El pago no se guardó ni cambió el vencimiento."
                            )
                        else:
                            st.session_state[f"latest_monthly_payment_{tab_index}"] = (
                                resident.id, resident.next_payment, resident.values["student_name"],
                            )
                            st.session_state[f"residents_notice_{tab_index}"] = (
                                "Pago guardado. Recibo de mensualidad disponible en el historial. "
                                "El próximo vencimiento avanzó 30 días."
                            )
                            st.rerun()
                if not pending_only:
                    _render_service_payment(store, resident.id, tab_index, key, today)
                with st.popover("Baja y entrega de depósito"):
                    discount_input = "0"
                    keys_returned = None
                    inspection_ok = None
                    deduction_reason = ""
                    if deposit is not None:
                        st.caption(
                            "La devolución se registra en la misma moneda del depósito (USD). "
                            "Sin novedades y con las llaves entregadas, el reembolso es completo. "
                            "Si faltan llaves o hay novedades, indica el descuento de la administración."
                        )
                        keys_returned = st.checkbox(
                            "La residente entregó las llaves", key=f"{key}_refund_keys",
                        )
                        inspection_ok = st.checkbox(
                            "Habitación y bienes revisados SIN NOVEDAD",
                            key=f"{key}_refund_inspection",
                        )
                        if keys_returned and inspection_ok:
                            st.info("Llaves entregadas y SIN NOVEDAD: devolución completa, sin descuento.")
                        else:
                            discount_input = st.text_input(
                                "Descuento del depósito (USD)", value="00,00",
                                help="Ingresa el descuento manualmente. Si no hay descuento, indica 00,00.",
                                key=f"{key}_refund_deduction",
                            ).strip()
                            deduction_reason = st.text_area(
                                "Descuento por (motivo manual)",
                                help="Obligatorio cuando hay descuento. Incluye las llaves no entregadas, si aplica.",
                                key=f"{key}_refund_reason",
                            )
                        try:
                            original = Decimal(deposit["amount"] or "")
                            discount_preview = Decimal(discount_input.replace(",", "."))
                            if (
                                not original.is_finite() or original <= 0
                                or not discount_preview.is_finite()
                                or not 0 <= discount_preview <= original
                                or discount_preview != discount_preview.quantize(Decimal("0.01"))
                            ):
                                st.error("El descuento debe estar entre cero y el depósito, con máximo dos decimales.")
                            else:
                                st.info(
                                    f"Depósito original: USD {original:.2f}. "
                                    f"Total a reembolsar: USD {original - discount_preview:.2f}."
                                )
                        except InvalidOperation:
                            st.error("No se puede calcular la devolución: verifica el monto y el descuento.")
                    else:
                        st.warning("Sin depósito registrado: la baja no genera devolución ni recibo de entrega.")
                    with st.form(f"{key}_discharge"):
                        reason = st.text_input("Motivo de la baja")
                        refund_method = None
                        refund_confirmed = False
                        if deposit is not None:
                            refund_method = st.selectbox(
                                "Forma de devolución del depósito en dólares",
                                ["Selecciona una forma de devolución", "Efectivo", "Binance", "Zelle"],
                            )
                            refund_confirmed = st.checkbox(
                                "Confirmo la entrega del depósito y el descuento aplicado"
                            )
                        confirmed_discharge = st.checkbox(
                            "Confirmo la baja; se conservarán sus datos y planilla"
                        )
                        discharge = st.form_submit_button("Dar de baja")
                if discharge:
                    if not confirmed_discharge:
                        st.error("Confirma la baja para continuar.")
                    else:
                        try:
                            store.discharge(
                                resident.id, tab_index, today, reason,
                                keys_returned=keys_returned, inspection_ok=inspection_ok,
                                deduction=Decimal(discount_input.replace(",", ".")),
                                deduction_reason=deduction_reason, refund_method=refund_method,
                                refund_confirmed=refund_confirmed,
                            )
                        except InvalidOperation:
                            st.error("Ingresa un descuento válido, con máximo dos decimales.")
                        except ValueError as error:
                            st.error(str(error))
                        except (OSError, pymupdf.FileDataError, sqlite3.Error):
                            LOGGER.exception("Error al guardar la baja y el recibo de entrega del depósito")
                            st.error(
                                "No se pudo generar o guardar el recibo de entrega. "
                                "No se registró la baja ni la devolución; intenta de nuevo."
                            )
                        else:
                            st.session_state[f"residents_notice_{tab_index}"] = (
                                "Baja guardada. "
                                + (
                                    "Constancia de entrega de depósito disponible en Residentes de Baja."
                                    if deposit is not None else
                                    "Sin depósito registrado: no se registró devolución."
                                )
                            )
                            st.session_state[f"residence_view_{tab_index}"] = 3
                            st.session_state[f"latest_discharged_resident_{tab_index}"] = resident.id
                            st.session_state[f"resident_search_{tab_index}_False_True"] = ""
                            st.rerun()
            history = store.payment_history(resident.id, tab_index)
            if history or deposit is not None:
                st.markdown("**Historial de pagos**")
            if deposit is not None:
                st.markdown("**Pago de depósito**")
                st.table([{
                    "Pago registrado": date.fromisoformat(deposit["paid_on"]).strftime("%d/%m/%Y"),
                    "Moneda": deposit["currency"] or "No registrado",
                    "Cantidad recibida": deposit["amount"] or "No registrado",
                    "Forma de pago": deposit["payment_method"] or "No registrado",
                }])
                receipt = store.get_deposit_receipt(resident.id, tab_index)
                if receipt is not None:
                    st.download_button(
                        "Descargar recibo de depósito (PDF)",
                        data=receipt,
                        file_name=(
                            f"Recibo_Deposito_{store.receipt_number(resident.id, tab_index) or resident.id}.pdf"
                        ),
                        mime="application/pdf", key=f"{key}_deposit_receipt",
                    )
                else:
                    st.info("Este depósito anterior no tiene un recibo PDF guardado.")
            for item in history:
                st.markdown("**Pago de mensualidad**")
                st.table([
                    {
                        "Vencimiento cubierto": date.fromisoformat(
                            item["due_date"]
                        ).strftime("%d/%m/%Y"),
                        "Pago registrado": date.fromisoformat(
                            item["paid_on"]
                        ).strftime("%d/%m/%Y"),
                        "Moneda": (
                            {"VES": "Bolívares", "USD": "Dólares"}.get(
                                item.get("currency"), "No registrado"
                            )
                        ),
                        "Cantidad recibida": (
                            f"{'Bs.' if item.get('currency') == 'VES' else '$'} "
                            f"{item['amount'].replace('.', ',')}"
                            if item.get("amount") and item.get("currency") in {"VES", "USD"}
                            else "No registrado"
                        ),
                        "Forma de pago": item.get("payment_method") or "—",
                        "Monto base (USD)": item.get("amount_usd") or "—",
                        "Mensualidad (USD)": item.get("monthly_amount_usd") or "—",
                        "Días de retardo": item.get("late_days") or "—",
                        "Mora (USD)": item.get("late_fee_usd") or "—",
                        "Banco receptor": item.get("receiving_bank") or "—",
                        "Tasa BCV": (
                            f"Bs. {item['exchange_rate'].replace('.', ',')} "
                            f"(fecha {date.fromisoformat(item['exchange_rate_date']):%d/%m/%Y})"
                            if item.get("exchange_rate") and item.get("exchange_rate_date")
                            else "—"
                        ),
                    }
                ])
                due_date = date.fromisoformat(item["due_date"])
                receipt = store.get_payment_receipt(resident.id, tab_index, due_date)
                if receipt is not None:
                    st.download_button(
                        f"Descargar recibo de mensualidad — {due_date:%d/%m/%Y} (PDF)",
                        data=receipt,
                        file_name=(
                            f"Recibo_Mensualidad_{store.receipt_number(resident.id, tab_index, due_date) or due_date.isoformat()}.pdf"
                        ),
                        mime="application/pdf",
                        key=f"{key}_monthly_receipt_{due_date.isoformat()}",
                    )
                else:
                    st.caption(
                        f"El pago con vencimiento {due_date:%d/%m/%Y} "
                        "es anterior a los recibos de mensualidad y no tiene PDF guardado."
                    )
            for service in store.service_payment_history(resident.id, tab_index):
                st.markdown("**Pago por otros Servicios**")
                received = Decimal(service["amount"])
                st.table([{
                    "Concepto": service["concept"],
                    "Pago registrado": date.fromisoformat(service["paid_on"]).strftime("%d/%m/%Y"),
                    "Moneda": service["currency"],
                    "Cantidad recibida": (
                        f"Bs. {format_bolivars(received)}" if service["currency"] == "VES"
                        else f"USD {received:.2f}".replace(".", ",")
                    ),
                    "IVA (16%) (Bs.)": (
                        format_bolivars(calculate_receipt_vat(received))
                        if service["currency"] == "VES" else "—"
                    ),
                    "N.º de factura": service["invoice_number"] or "Sin indicar",
                    "Forma de pago": service["payment_method"] or "—",
                    "Banco receptor": service["receiving_bank"] or "—",
                    "Tasa BCV": service["exchange_rate"] or "—",
                    "Fecha de la tasa": service["exchange_rate_date"] or "—",
                }])
                st.download_button(
                    f"Descargar recibo de servicio — {service['concept']} — {service['receipt_number']} (PDF)",
                    data=bytes(service["receipt_pdf"]),
                    file_name=f"Recibo_Servicio_{service['receipt_number']}.pdf",
                    mime="application/pdf", key=f"{key}_service_receipt_{service['id']}",
                )


st.set_page_config(
    page_title="Portal Administrativo Control y Administración",
    page_icon="🏛️",
    layout="wide",
)

st.markdown(
    """
    <style>
    :root {
        --ink: #142a36;
        --muted: #5f727b;
        --navy: #102b3a;
        --green: #28705b;
        --paper: #f4f6f3;
        --line: #e1e8e3;
    }
    .stApp {
        background: var(--paper);
        color: var(--ink);
        font-family: Inter, Arial, sans-serif;
    }
    [data-testid="stHeader"] { background: transparent; }
    [data-testid="stMainBlockContainer"] {
        max-width: 1180px;
        padding-top: 1.5rem;
    }
    .prototype-note {
        background: #fff8e7;
        border: 1px solid #ead9a8;
        border-radius: 8px;
        color: #66521e;
        font-size: 0.83rem;
        padding: 0.7rem 1rem;
        margin-bottom: 1rem;
    }
    .masthead {
        align-items: center;
        border-bottom: 1px solid var(--line);
        display: flex;
        justify-content: space-between;
        padding: 0.45rem 0 1.15rem;
    }
    .brand {
        color: var(--navy);
        font-family: Inter, Arial, sans-serif;
        font-size: 1rem;
        font-weight: 800;
        letter-spacing: 0.015em;
        line-height: 1.3;
        max-width: 450px;
    }
    .brand small {
        color: var(--muted);
        display: block;
        font-family: Inter, Arial, sans-serif;
        font-size: 0.72rem;
        font-weight: 500;
        letter-spacing: 0.08em;
        margin-top: 0.2rem;
        text-transform: uppercase;
    }
    .masthead-label {
        color: var(--muted);
        font-size: 0.8rem;
        text-align: right;
    }
    .hero {
        background: linear-gradient(115deg, #102b3a 0%, #174b4c 100%);
        border-radius: 12px;
        color: white;
        margin: 1.4rem 0 1.7rem;
        overflow: hidden;
        padding: clamp(2rem, 6vw, 4.7rem);
        position: relative;
    }
    .hero:after {
        border: 1px solid rgba(255,255,255,0.1);
        border-radius: 50%;
        content: "";
        height: 340px;
        position: absolute;
        right: -80px;
        top: -125px;
        width: 340px;
    }
    .eyebrow {
        color: #b6d8c9;
        font-size: 0.77rem;
        font-weight: 700;
        letter-spacing: 0.15em;
        text-transform: uppercase;
    }
    .hero h1 {
        color: white;
        font-family: Inter, Arial, sans-serif;
        font-size: clamp(2rem, 5vw, 3.25rem);
        letter-spacing: -0.04em;
        line-height: 1.12;
        margin: 0.75rem 0;
        max-width: 650px;
    }
    .hero p {
        color: #e0ece6;
        font-size: 1rem;
        line-height: 1.65;
        margin: 0;
        max-width: 590px;
    }
    .section-heading {
        color: var(--navy);
        font-family: Inter, Arial, sans-serif;
        font-size: 1.45rem;
        font-weight: 800;
        letter-spacing: -0.025em;
        margin: 0.45rem 0 0.25rem;
    }
    .section-subtitle {
        color: var(--muted);
        font-size: 0.92rem;
        margin: 0 0 1rem;
    }
    .info-card {
        background: white;
        border: 1px solid var(--line);
        border-radius: 10px;
        height: 100%;
        min-height: 150px;
        padding: 1.25rem;
    }
    .card-kicker {
        color: var(--green);
        font-size: 0.72rem;
        font-weight: 700;
        letter-spacing: 0.1em;
        text-transform: uppercase;
    }
    .info-card h3 {
        color: var(--navy);
        font-family: 'Manrope', sans-serif;
        font-size: 1.1rem;
        margin: 0.6rem 0 0.4rem;
    }
    .info-card p {
        color: var(--muted);
        font-size: 0.88rem;
        line-height: 1.55;
        margin: 0;
    }
    .footer-note {
        border-top: 1px solid var(--line);
        color: var(--muted);
        font-size: 0.78rem;
        margin-top: 1.4rem;
        padding: 1rem 0;
    }
    div[data-testid="stTabs"] button {
        color: var(--muted);
        font-weight: 600;
    }
    div[data-testid="stTabs"] button[aria-selected="true"] {
        color: var(--green);
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    """
    <div class="prototype-note">
        <strong></strong>
    </div>
    <div class="masthead">
        <div class="brand">
        </div>
    </div>
    <section class="hero">
        <div class="eyebrow">Portal Administrativo Control y Administración</div>
        <h1>RESIDENCIA ESTUDIANTIL</h1>
        <p>
        </p>
    </section>
    """,
    unsafe_allow_html=True,
)

try:
    resident_store = ResidentStore()
except (sqlite3.Error, OSError):
    LOGGER.exception("Error al abrir la base de datos de residentes")
    st.error(
        "No se pudo abrir la base de datos. No se guardarán ingresos hasta resolver "
        "el problema de almacenamiento."
    )
    st.stop()

tabs = st.tabs(["Residencia Carmen Luisa", "Residencia las Acacias"])

for tab_index, tab in enumerate(tabs):
    with tab:
        columns = st.columns(5)
        for card_index, (column, label) in enumerate(zip(
            columns,
            [
                "Nuevo Ingreso", "Residentes Activas", "Pagos Pendientes",
                "Residentes de Baja", "Pagos recibidos en el mes",
            ],
        )):
            with column:
                if st.button(label, key=f"residencia_action_{tab_index}_{card_index}"):
                    st.session_state[f"residence_view_{tab_index}"] = card_index
        view = st.session_state.get(f"residence_view_{tab_index}", 0)
        try:
            if view == 0:
                _render_admission_form(tab_index, resident_store)
            elif view == 4:
                _render_monthly_received(tab_index, resident_store)
            elif view in (1, 2, 3):
                _render_residents(
                    tab_index, resident_store, pending_only=view == 2,
                    inactive=view == 3,
                )
        except (sqlite3.Error, OSError):
            LOGGER.exception("Error de almacenamiento en la vista de residentes")
            st.error(
                "No se pudo completar la operación de almacenamiento. "
                "No se ha confirmado ningún cambio; vuelve a consultar el registro "
                "antes de intentarlo de nuevo."
            )
