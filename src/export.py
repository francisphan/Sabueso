"""CSV and PDF generation + Slack file upload helpers."""

from __future__ import annotations

import csv
import logging
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from slack_sdk import WebClient

log = logging.getLogger(__name__)


def generate_csv(rows: list[list[str]], filename: str = "export.csv") -> Path:
    """Write rows (first row = header) to a temp CSV file."""
    path = Path(tempfile.gettempdir()) / filename
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(rows)
    log.info("CSV written to %s (%d rows)", path, len(rows))
    return path


def generate_pdf(
    rows: list[list[str]],
    filename: str = "export.pdf",
    title: str = "Export",
) -> Path:
    """Render rows into a simple table PDF."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet

    path = Path(tempfile.gettempdir()) / filename
    doc = SimpleDocTemplate(str(path), pagesize=LETTER)
    styles = getSampleStyleSheet()

    elements = [
        Paragraph(title, styles["Title"]),
        Spacer(1, 0.25 * inch),
    ]

    if rows:
        table = Table(rows)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#8B4513")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.whitesmoke, colors.white]),
        ]))
        elements.append(table)

    doc.build(elements)
    log.info("PDF written to %s", path)
    return path


def upload_file_to_thread(
    client: "WebClient",
    channel: str,
    thread_ts: str | None,
    filepath: Path,
    title: str = "export",
    initial_comment: str = "Here's your export:",
):
    """Upload a file to a Slack channel/thread using files_upload_v2."""
    client.files_upload_v2(
        channel=channel,
        thread_ts=thread_ts,
        file=str(filepath),
        title=title,
        initial_comment=initial_comment,
    )
    log.info("Uploaded %s to channel=%s thread=%s", filepath.name, channel, thread_ts)
