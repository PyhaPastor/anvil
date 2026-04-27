"""Export service — generates CSV and PDF reports gated by user role."""
from __future__ import annotations
import csv
import io
from datetime import datetime
from typing import List

from fastapi.responses import StreamingResponse

from ..models.hash_list import Hash, HashList
from ..models.job import Job
from ..models.user import User


def _sanitise_cell(value: str | None) -> str:
    """Prevent CSV injection by prefixing dangerous characters."""
    if value is None:
        return ""
    s = str(value)
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        s = "'" + s
    return s


def export_job_csv(job: Job, hashes: List[Hash], user: User) -> StreamingResponse:
    """
    Export cracked hashes as CSV.
    - Admin/Analyst/Viewer: full data (username, hash, plaintext)
    - Presentation: summary row only — no credentials
    """
    buf = io.StringIO()
    writer = csv.writer(buf)

    if user.can_view_credentials():
        writer.writerow(["username", "hash", "plaintext", "cracked_at"])
        for h in hashes:
            if h.plaintext is not None:
                writer.writerow([
                    _sanitise_cell(h.username),
                    _sanitise_cell(h.hash_value),
                    _sanitise_cell(h.plaintext),
                    h.cracked_at.isoformat() if h.cracked_at else "",
                ])
    else:
        writer.writerow(["metric", "value"])
        writer.writerow(["Job", _sanitise_cell(job.name)])
        writer.writerow(["Total hashes", job.total_hashes])
        writer.writerow(["Cracked", job.cracked_count])
        writer.writerow(["Crack rate", f"{job.crack_rate_pct}%"])
        writer.writerow(["Status", job.status.value])

    buf.seek(0)
    filename = f"anvil_export_{job.id}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def export_job_pdf(job: Job, hashes: List[Hash], user: User, customer_name: str) -> StreamingResponse:
    """Generate a PDF summary report."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from reportlab.lib import colors

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=2*cm, rightMargin=2*cm,
                             topMargin=2*cm, bottomMargin=2*cm)
    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph("Anvil — Cracking Report", styles["Title"]))
    story.append(Spacer(1, 0.4*cm))
    story.append(Paragraph(f"Customer: {customer_name}", styles["Normal"]))
    story.append(Paragraph(f"Job: {job.name}", styles["Normal"]))
    story.append(Paragraph(f"Status: {job.status.value}", styles["Normal"]))
    story.append(Paragraph(
        f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}", styles["Normal"]
    ))
    story.append(Spacer(1, 0.4*cm))

    # Summary table (always included)
    summary_data = [
        ["Metric", "Value"],
        ["Total hashes", str(job.total_hashes)],
        ["Cracked", str(job.cracked_count)],
        ["Crack rate", f"{job.crack_rate_pct}%"],
        ["Duration", f"{job.duration_seconds or 0}s"],
    ]
    ts = TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2C2C2A")),
        ("TEXTCOLOR",  (0, 0), (-1, 0), colors.white),
        ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.HexColor("#F1EFE8"), colors.white]),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#B4B2A9")),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
    ])
    t = Table(summary_data, colWidths=[8*cm, 8*cm])
    t.setStyle(ts)
    story.append(t)

    # Credential detail table — only for roles that can see credentials
    if user.can_view_credentials():
        story.append(Spacer(1, 0.6*cm))
        story.append(Paragraph("Cracked credentials", styles["Heading2"]))
        rows = [["Username", "Hash (truncated)", "Plaintext", "Cracked at"]]
        for h in hashes:
            if h.plaintext is not None:
                rows.append([
                    h.username or "",
                    (h.hash_value[:24] + "…") if len(h.hash_value) > 24 else h.hash_value,
                    h.plaintext,
                    h.cracked_at.strftime("%Y-%m-%d %H:%M") if h.cracked_at else "",
                ])
        if len(rows) > 1:
            cred_ts = TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#185FA5")),
                ("TEXTCOLOR",  (0, 0), (-1, 0), colors.white),
                ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.HexColor("#E6F1FB"), colors.white]),
                ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#B5D4F4")),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("WORDWRAP", (0, 0), (-1, -1), True),
            ])
            ct = Table(rows, colWidths=[4*cm, 5*cm, 4*cm, 4*cm])
            ct.setStyle(cred_ts)
            story.append(ct)
        else:
            story.append(Paragraph("No hashes cracked in this job.", styles["Normal"]))

    doc.build(story)
    buf.seek(0)
    filename = f"anvil_report_{job.id}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.pdf"
    return StreamingResponse(
        iter([buf.read()]),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
