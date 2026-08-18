"""CSV/PDF helpers for stock inventory age tables."""

from __future__ import annotations

import csv
import datetime as dt
import io
from typing import Any

from services.farms import HERD_FARM_OPTIONS

PDF_CONTENT_TYPE = "application/pdf"


def export_columns(selected_farms: list[str]) -> list[str]:
    farms = [f for f in HERD_FARM_OPTIONS if f in selected_farms]
    return farms or list(HERD_FARM_OPTIONS)


def format_import_note(latest_import: str | None) -> str:
    if not latest_import:
        return "No inventory import found"
    try:
        stamp = dt.datetime.fromisoformat(latest_import)
        return f"Latest import: {stamp.strftime('%d/%m/%Y %H:%M')}"
    except ValueError:
        return f"Latest import: {latest_import}"


def build_age_inventory_csv(report: dict[str, Any], selected_farms: list[str]) -> str:
    farms = export_columns(selected_farms)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Months Old", *farms, "Total"])
    for row in report.get("rows", []):
        writer.writerow(
            [row["months_old"], *[row.get(f, 0) for f in farms], row.get("total", 0)]
        )
    grand = report.get("grand_total", {})
    writer.writerow(
        ["Grand Total", *[grand.get(f, 0) for f in farms], grand.get("total", 0)]
    )
    return buffer.getvalue()


def build_age_inventory_pdf(
    report: dict[str, Any],
    selected_farms: list[str],
    *,
    title: str,
    empty_message: str,
    extra_meta: list[str] | None = None,
) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    farms = export_columns(selected_farms)
    styles = getSampleStyleSheet()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        title=title,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
    )

    bounds = report.get("age_bounds") or {}
    meta_bits = [
        f"Farms: {', '.join(farms)}",
        *(extra_meta or []),
        f"Age range: {bounds.get('min', 0)}–{bounds.get('max', 0)} months",
        format_import_note(report.get("latest_import")),
        f"Generated: {dt.datetime.now().strftime('%d/%m/%Y %H:%M')}",
    ]

    elements: list[Any] = [
        Paragraph(title, styles["Title"]),
        Paragraph("  |  ".join(meta_bits), styles["Normal"]),
        Spacer(1, 8 * mm),
    ]

    header = ["Months Old", *farms, "Total"]
    table_data: list[list[Any]] = [header]
    for row in report.get("rows", []):
        table_data.append(
            [str(row["months_old"]), *[str(row.get(f, 0)) for f in farms], str(row.get("total", 0))]
        )
    grand = report.get("grand_total", {})
    table_data.append(
        ["Grand Total", *[str(grand.get(f, 0)) for f in farms], str(grand.get("total", 0))]
    )

    table = Table(table_data, repeatRows=1, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e3a5f")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("ALIGN", (0, 0), (0, -1), "LEFT"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#f3f6f9")]),
                ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#eef2f6")),
                ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                ("LINEABOVE", (0, -1), (-1, -1), 1, colors.HexColor("#9ca3af")),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#d8dee4")),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    elements.append(table)

    if not report.get("rows"):
        elements.append(Spacer(1, 6 * mm))
        elements.append(Paragraph(empty_message, styles["Italic"]))

    doc.build(elements)
    return buffer.getvalue()
