from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pymupdf
import pytest
from streamlit.testing.v1 import AppTest

from resident_export import generate_resident_report
from resident_store import Resident, ResidentStore


TODAY = date.today()
APP_PATH = Path(__file__).resolve().parents[1] / "app.py"


def resident(number=1, *, residence=0, inactive=False):
    return Resident(
        id=number, residence=residence,
        values={
            "student_name": f"RESIDENTE PRUEBA {number:03d}",
            "student_id": f"V-{10000000 + number}",
            "student_mobile": "0412-1234567",
            "room_number": "07", "bed_number": "02",
            "admission_date": "02/01/2026",
        },
        documents=[False] * 8, admission_date=date(2026, 1, 2),
        next_payment=TODAY, discharged_on=TODAY if inactive else None,
        discharge_reason="CULMINACION DE ESTUDIOS <APROBADA> & ENTREGA" if inactive else None,
    )


@pytest.mark.parametrize("residence", [0, 1])
@pytest.mark.parametrize("inactive", [False, True])
def test_pdf_has_requested_columns_in_order_and_resident_data(residence, inactive):
    entry = resident(residence=residence, inactive=inactive)
    pdf = generate_resident_report(residence, [entry], TODAY, inactive=inactive)
    with pymupdf.open(stream=pdf, filetype="pdf") as document:
        text = " ".join(document[0].get_text().split())
        assert len(document) == 1
    columns = ["Nro.", "Fecha de ingreso", "Nombre y apellidos", "Cédula de identidad"]
    columns += (
        ["Fecha de egreso", "Motivos del egreso"]
        if inactive else ["Nro. de telf. celular", "Habitación", "Cama"]
    )
    assert [text.index(column) for column in columns] == sorted(
        text.index(column) for column in columns
    )
    for value in ("02/01/2026", entry.values["student_name"], entry.values["student_id"]):
        assert value in text
    assert ("CARMEN LUISA" if residence == 0 else "LAS ACACIAS") in text
    assert "Total de residentes: 1" in text
    if inactive:
        assert TODAY.strftime("%d/%m/%Y") in text
        assert entry.discharge_reason in text
        assert "Habitación" not in text
    else:
        assert "0412-1234567" in text
        assert "07" in text and "02" in text
        assert "Motivos del egreso" not in text


@pytest.mark.parametrize("inactive", [False, True])
def test_pdf_alignment_and_bold_headers_match_column_types(inactive):
    entry = replace(
        resident(inactive=inactive),
        values={**resident().values, "student_name": "ANA PEREZ 2"},
        discharge_reason="FIN DEL PERIODO 2026",
    )
    pdf = generate_resident_report(0, [entry], TODAY, inactive=inactive)
    with pymupdf.open(stream=pdf, filetype="pdf") as document:
        page = document[0]
        cells = page.find_tables().tables[0].rows[0].cells
        spans = [
            span for block in page.get_text("dict")["blocks"]
            for line in block.get("lines", []) for span in line["spans"]
            if 100 < span["bbox"][1] < 145
        ]
        headers = [span for span in spans if "Bold" in span["font"]]
        values = [span for span in spans if "Bold" not in span["font"]]
        assert len(headers) == len(cells) == (6 if inactive else 7)
        assert len(values) == len(cells)
        centered = (0, 1, 3, 4) if inactive else (0, 1, 3, 4, 5, 6)
        for index, (cell, header, value) in enumerate(zip(cells, headers, values)):
            left, _, right, _ = cell
            midpoint = (left + right) / 2
            assert (header["bbox"][0] + header["bbox"][2]) / 2 == pytest.approx(
                midpoint, abs=0.5,
            )
            if index in centered:
                assert (value["bbox"][0] + value["bbox"][2]) / 2 == pytest.approx(
                    midpoint, abs=0.5,
                )
            else:
                assert value["bbox"][0] == pytest.approx(left + 3.25, abs=0.5)


@pytest.mark.parametrize("inactive", [False, True])
def test_pdf_paginates_without_losing_rows_and_repeats_headers(inactive):
    entries = [resident(number, inactive=inactive) for number in range(1, 101)]
    pdf = generate_resident_report(0, entries, TODAY, inactive=inactive)
    with pymupdf.open(stream=pdf, filetype="pdf") as document:
        assert len(document) > 1
        text = " ".join(" ".join(page.get_text().split()) for page in document)
        for page in document:
            assert "Cédula de identidad" in " ".join(page.get_text().split())
        for entry in entries:
            assert text.count(entry.values["student_id"]) == 1
        assert "Total de residentes: 100" in text
        assert f"Página {len(document)} de {len(document)}" in text


def test_empty_pdf_and_legacy_missing_fields_are_explicit():
    for entries, expected in (
        ([], "No hay residentes que mostrar."),
        ([replace(resident(), values={"student_name": "PRUEBA", "student_id": "V-123"})],
         "Sin indicar"),
    ):
        pdf = generate_resident_report(0, entries, TODAY)
        with pymupdf.open(stream=pdf, filetype="pdf") as document:
            assert expected in document[0].get_text()


@pytest.mark.parametrize("can_write", [True, False])
@pytest.mark.parametrize("residence", [0, 1])
@pytest.mark.parametrize("inactive", [False, True])
def test_ui_exports_only_selected_residence_status_and_search(
    tmp_path, can_write, residence, inactive,
):
    store = ResidentStore(tmp_path / "residents.sqlite3")
    for area in (0, 1):
        for number in (1, 2, 3):
            entry = resident(number, residence=area)
            resident_id = store.add(area, entry.values, entry.documents, b"PDF", None)
            if number in (2, 3):
                store.discharge(resident_id, area, TODAY, "FIN DE ESTUDIOS")
    access = SimpleNamespace(
        role="administrator" if can_write else "viewer",
        can_write=can_write, require_write=lambda: None,
    )
    with (
        patch("resident_store.ResidentStore", return_value=store),
        patch("portal_auth.require_login", return_value=access),
        patch("portal_auth.select_residence_area", return_value=(0, 1)),
        patch("bcv_rate.fetch_bcv_rate", return_value=(Decimal("100"), TODAY)),
        patch("resident_export.generate_resident_report", wraps=generate_resident_report) as export,
    ):
        view = 3 if inactive else 1
        app = AppTest.from_file(str(APP_PATH), default_timeout=30).run()
        app.button(key=f"residencia_action_{residence}_{view}").click().run()
        assert not app.exception
        label = f"Descargar lista de residentes {'de baja' if inactive else 'activas'} (PDF)"
        assert any(item.label == label for item in app.tabs[residence].get("download_button"))
        selected = [
            call for call in export.call_args_list
            if call.args[0] == residence and call.kwargs["inactive"] == inactive
        ][-1]
        assert len(selected.args[1]) == (2 if inactive else 1)
        assert all(entry.residence == residence for entry in selected.args[1])
        search_key = (
            f"resident_search_{residence}_False_{inactive}"
            if can_write else f"readonly_search_{residence}_{view}"
        )
        app.text_input(key=search_key).input("PRUEBA 002" if inactive else "PRUEBA 001").run()
        assert not app.exception
        selected = [
            call for call in export.call_args_list
            if call.args[0] == residence and call.kwargs["inactive"] == inactive
        ][-1]
        assert len(selected.args[1]) == 1
        app.text_input(key=search_key).input("SIN COINCIDENCIAS").run()
        assert not app.exception
        selected = [
            call for call in export.call_args_list
            if call.args[0] == residence and call.kwargs["inactive"] == inactive
        ][-1]
        assert selected.args[1] == []


def test_pdf_generation_error_is_visible_and_does_not_offer_download(tmp_path, caplog):
    store = ResidentStore(tmp_path / "residents.sqlite3")
    access = SimpleNamespace(role="viewer", can_write=False, require_write=lambda: None)
    with (
        patch("resident_store.ResidentStore", return_value=store),
        patch("portal_auth.require_login", return_value=access),
        patch("portal_auth.select_residence_area", return_value=(0,)),
        patch("bcv_rate.fetch_bcv_rate", return_value=(Decimal("100"), TODAY)),
        patch("resident_export.generate_resident_report", side_effect=ValueError("Fila demasiado larga")),
    ):
        app = AppTest.from_file(str(APP_PATH), default_timeout=30).run()
        assert not app.exception
        assert any("No se pudo generar el PDF" in item.value for item in app.error)
        assert "Fila demasiado larga" in caplog.text
        assert not app.get("download_button")