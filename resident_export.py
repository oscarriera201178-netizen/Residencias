from datetime import date

from expense_export import generate_table_report
from resident_store import Resident


def generate_resident_report(
    residence: int, residents: list[Resident], printed_on: date, *, inactive: bool = False,
) -> bytes:
    headers = [
        "Nro.", "Fecha de ingreso", "Nombre y apellidos", "Cédula de identidad",
    ]
    headers += (
        ["Fecha de egreso", "Motivos del egreso"]
        if inactive else ["Nro. de telf. celular", "Habitación", "Cama"]
    )
    rows = []
    for number, resident in enumerate(residents, start=1):
        row = [
            str(number), resident.admission_date.strftime("%d/%m/%Y"),
            resident.values["student_name"], resident.values["student_id"],
        ]
        row += (
            [
                resident.discharged_on.strftime("%d/%m/%Y")
                if resident.discharged_on else "Sin indicar",
                resident.discharge_reason or "Sin indicar",
            ]
            if inactive else [
                resident.values.get(field) or "Sin indicar"
                for field in ("student_mobile", "room_number", "bed_number")
            ]
        )
        rows.append(row)
    if not rows:
        rows = [["", "", "No hay residentes que mostrar.", *([""] * (len(headers) - 3))]]
    return generate_table_report(
        residence, headers, rows, f"<p><b>Total de residentes: {len(residents)}</b></p>",
        "RESIDENTES DE BAJA" if inactive else "RESIDENTES ACTIVAS", printed_on,
        centered_columns=(0, 1, 3, 4) if inactive else (0, 1, 3, 4, 5, 6),
    )