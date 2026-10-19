from pathlib import Path

import pymupdf
import streamlit as st

ADMISSION_TEMPLATE = Path(__file__).with_name(
    "Planilla_de_Ingreso_Residencia_Carmen_Luisa.pdf"
)


def _insert_pdf_value(page, value, rect, cover_underline=False):
    value = " ".join(value.split())
    if not value:
        return

    text_rect = pymupdf.Rect(rect)
    if cover_underline:
        page.draw_rect(text_rect, color=None, fill=(1, 1, 1))

    for font_size in (7.5, 7, 6.5, 6, 5.5):
        remaining = page.insert_textbox(
            text_rect,
            value,
            fontname="helv",
            fontsize=font_size,
            color=(0, 0, 0),
        )
        if remaining >= 0:
            return

    raise ValueError("Uno de los datos es demasiado largo para el espacio disponible.")


def _generate_admission_pdf(values, submitted_documents, photo_bytes):
    with pymupdf.open(ADMISSION_TEMPLATE) as document:
        page = document[0]
        placements = {
            "student_name": (160, 299, 359, 316),
            "student_id": (480, 299, 559, 316),
            "birth_date": (111, 320, 169, 339),
            "age": (201, 320, 225, 339),
            "student_mobile": (289, 320, 360, 339),
            "student_landline": (408, 320, 559, 339),
            "emergency_phone": (135, 342, 226, 360),
            "email": (262, 342, 559, 360),
            "student_address": (177, 363, 559, 382),
            "city": (92, 407, 305, 426),
            "state": (348, 407, 559, 426),
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
        for field_name, rect in placements.items():
            _insert_pdf_value(page, values.get(field_name, ""), rect)

        if photo_bytes:
            page.insert_image(
                pymupdf.Rect(458, 88, 557, 132),
                stream=photo_bytes,
                keep_proportion=True,
            )

        checkbox_positions = [
            (53, 172),
            (309, 172),
            (53, 205),
            (309, 205),
            (53, 237),
            (309, 237),
            (53, 257),
            (309, 257),
        ]
        for checked, (x, y) in zip(submitted_documents, checkbox_positions):
            if checked:
                page.draw_line((x, y), (x + 2.5, y + 2.5), width=1.2)
                page.draw_line((x + 2.5, y), (x, y + 2.5), width=1.2)

        signature_page = document[1]
        _insert_pdf_value(
            signature_page,
            values.get("student_id", ""),
            (129, 379, 261, 393),
            cover_underline=True,
        )
        _insert_pdf_value(
            signature_page,
            values.get("representative_id", ""),
            (384, 379, 518, 393),
            cover_underline=True,
        )
        _insert_pdf_value(
            signature_page,
            values.get("admission_date", ""),
            (130, 505, 259, 520),
            cover_underline=True,
        )
        _insert_pdf_value(
            signature_page,
            values.get("room_number", ""),
            (273, 569, 312, 584),
            cover_underline=True,
        )
        _insert_pdf_value(
            signature_page,
            values.get("bed_number", ""),
            (367, 569, 406, 584),
            cover_underline=True,
        )

        return document.tobytes(garbage=3, deflate=True)


def _render_admission_form(tab_index):
    form_key = f"admission_form_{tab_index}"
    output_key = f"admission_pdf_{tab_index}"
    values = {}

    st.subheader("Planilla de admisión e ingreso")
    st.caption(
        "Completa los datos para generar una copia de la planilla original. "
        "La plantilla y las firmas originales no se modifican."
    )

    with st.form(form_key):
        st.markdown("#### Información de la estudiante")
        student_columns = st.columns(2)
        values["student_name"] = student_columns[0].text_input(
            "Nombres y apellidos *", key=f"{form_key}_student_name"
        )
        values["student_id"] = student_columns[1].text_input(
            "Cédula de identidad *", key=f"{form_key}_student_id"
        )

        student_columns = st.columns(3)
        values["birth_date"] = student_columns[0].text_input(
            "Fecha de nacimiento *", placeholder="DD/MM/AAAA", key=f"{form_key}_birth_date"
        )
        values["age"] = student_columns[1].text_input(
            "Edad *", key=f"{form_key}_age"
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
        values["city"] = student_columns[0].text_input(
            "Ciudad *", key=f"{form_key}_city"
        )
        values["state"] = student_columns[1].text_input(
            "Estado *", key=f"{form_key}_state"
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
        values["mother_address"] = st.text_input(
            "Dirección actual (madre o tutora)", key=f"{form_key}_mother_address"
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
        values["father_address"] = st.text_input(
            "Dirección actual (padre o tutor)", key=f"{form_key}_father_address"
        )
        values["emergency_contacts"] = st.text_input(
            "Teléfono y contactos de emergencia (vecino, abuelo, tío)",
            key=f"{form_key}_emergency_contacts",
        )
        values["representative_id"] = st.text_input(
            "Cédula del representante legal *", key=f"{form_key}_representative_id"
        )

        st.markdown("#### Recaudos consignados")
        document_labels = [
            "Ser mayor de edad",
            "Certificado de antecedentes penales",
            "Constancia de estudios o inscripción universitaria",
            "Cédula de los padres o representante legal",
            "Foto tipo carnet reciente",
            "Dos referencias personales del representante",
            "Cédula de identidad de la estudiante",
            "Constancia de trabajo de los padres o representante",
        ]
        document_columns = st.columns(2)
        submitted_documents = [
            document_columns[index % 2].checkbox(
                label, key=f"{form_key}_document_{index}"
            )
            for index, label in enumerate(document_labels)
        ]

        st.markdown("#### Datos administrativos")
        admin_columns = st.columns(3)
        values["admission_date"] = admin_columns[0].text_input(
            "Fecha de ingreso", placeholder="DD/MM/AAAA", key=f"{form_key}_admission_date"
        )
        values["room_number"] = admin_columns[1].text_input(
            "Habitación N.º", key=f"{form_key}_room_number"
        )
        values["bed_number"] = admin_columns[2].text_input(
            "Cama N.º", key=f"{form_key}_bed_number"
        )
        photo = st.file_uploader(
            "Foto tipo carnet (opcional)",
            type=["jpg", "jpeg", "png"],
            key=f"{form_key}_photo",
        )

        accepted_commitments = st.checkbox(
            "Confirmo que leí las normas de convivencia. La planilla descargada "
            "deberá imprimirse para firmarla.",
            key=f"{form_key}_accepted_commitments",
        )
        submitted = st.form_submit_button(
            "Generar planilla completada", type="primary"
        )

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
            "Cédula del representante legal": values["representative_id"],
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
                st.session_state[output_key] = _generate_admission_pdf(
                    values,
                    submitted_documents,
                    photo.getvalue() if photo else None,
                )
            except (pymupdf.FileDataError, ValueError):
                st.error(
                    "No se pudo generar el PDF. Revisa la foto adjunta y que cada "
                    "dato quepa en su espacio de la planilla."
                )
            else:
                st.success("La copia completada está lista para descargar.")

    if output_key in st.session_state:
        st.download_button(
            "Descargar planilla completada (PDF)",
            data=st.session_state[output_key],
            file_name="Planilla_de_Ingreso_Completada.pdf",
            mime="application/pdf",
            key=f"{form_key}_download",
        )
        st.caption(
            "Las líneas de firma quedan en blanco para firmar la planilla impresa."
        )

    if st.button("Cerrar formulario", key=f"{form_key}_close"):
        st.session_state.pop(output_key, None)
        st.session_state["active_new_entry_tab"] = None
        st.rerun()


st.set_page_config(
    page_title="Portal Administrativo para el Control y Administración de:",
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
    .contact-strip {
        background: #e9efeb;
        border-radius: 10px;
        color: var(--ink);
        margin: 1.8rem 0 0.7rem;
        padding: 1.2rem 1.4rem;
    }
    .contact-strip strong { color: var(--navy); }
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
        <div class="eyebrow">Portal Administrativo para el Control y Administración de:</div>
        <h1>RESIDENCIA ESTUDIANTIL</h1>
        <p>
        </p>
    </section>
    """,
    unsafe_allow_html=True,
)

tabs = st.tabs(["Residencia Carmen Luisa", "Residencia las Acacias"])

for tab_index, tab in enumerate(tabs):
    with tab:
        columns = st.columns(3)
        for card_index, (column, kicker, title, text) in enumerate(zip(
            columns,
            ["Nuevo Ingreso", "Residentes Activas", "Pagos Pendientes"],
            ["Residencia Carmen Luisa", "Estructura organizativa", "Documentos públicos"],
            [
                "Presentación, misión e información general, pendiente de validación institucional.",
                "Un espacio para publicar dependencias, autoridades y funciones verificadas.",
                "Acceso a informes, normativa y datos públicos autorizados.",
            ],
        )):
            with column:
                if st.button(kicker, key=f"residencia_action_{tab_index}_{card_index}"):
                    st.session_state["active_new_entry_tab"] = (
                        tab_index if card_index == 0 else None
                    )
                    st.session_state.pop(f"admission_pdf_{tab_index}", None)
                kicker_html = (
                    "" if card_index < 3 else f'<div class="card-kicker">{kicker}</div>'
                )
                st.markdown(
                    f"""
                    <article class="info-card">
                        {kicker_html}
                        <h3>{title}</h3>
                        <p>{text}</p>
                    </article>
                    """,
                    unsafe_allow_html=True,
                )
        if st.session_state.get("active_new_entry_tab") == tab_index:
            _render_admission_form(tab_index)

st.markdown(
    """
    <div class="contact-strip">
        <strong>¿Buscas información oficial?</strong><br>
        Verifica siempre la dirección web y los canales institucionales antes de compartir información personal.
    </div>
    """,
    unsafe_allow_html=True,
)
