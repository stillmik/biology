import html
import re
import uuid

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from ..core.config import GENERATED_FILES_DIRECTORY, MAX_FILE_CONTENT_SIZE
from ..infrastructure.database import create_generated_file_from_db


def select_generated_file_type(message: str) -> str:
    normalized_message = message.lower()
    return "txt" if any(value in normalized_message for value in [".txt", "txt file", "text file", "plain text"]) else "pdf"


def strip_inline_markdown(value: str) -> str:
    return re.sub(r"(`+|\*{1,3}|_+)", "", value).strip()


def truncate_generated_file_content(content: str, token_limit: int) -> str:
    return content[: max(1, token_limit * 3)]


def create_pdf_table(rows: list[list[str]], available_width: float, styles) -> Table:
    column_count = max(len(row) for row in rows)
    normalized_rows = [row + [""] * (column_count - len(row)) for row in rows]
    cells = [[Paragraph(html.escape(strip_inline_markdown(cell)).replace("\n", "<br/>"), styles["BodyText"]) for cell in row] for row in normalized_rows]
    table = Table(cells, colWidths=[available_width / column_count] * column_count, repeatRows=1)
    table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8edf5")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#1f2937")), ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#9ca3af")), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6), ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    return table


def create_pdf_response_file(path: Path, content: str) -> None:
    styles = getSampleStyleSheet()
    document = SimpleDocTemplate(str(path), pagesize=letter, rightMargin=0.5 * inch, leftMargin=0.5 * inch, topMargin=0.55 * inch, bottomMargin=0.55 * inch)
    story, lines, index = [], content.splitlines(), 0
    available_width = letter[0] - document.leftMargin - document.rightMargin
    while index < len(lines):
        line = lines[index]
        if line.strip().startswith("|") and index + 1 < len(lines) and re.fullmatch(r"\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*", lines[index + 1]):
            rows = [line.strip().strip("|").split("|")]
            index += 2
            while index < len(lines) and lines[index].strip().startswith("|"):
                rows.append(lines[index].strip().strip("|").split("|"))
                index += 1
            story.extend([create_pdf_table([[cell.strip() for cell in row] for row in rows], available_width, styles), Spacer(1, 10)])
            continue
        heading = re.fullmatch(r"\s*#{1,6}\s+(.+)", line)
        if heading:
            story.extend([Paragraph(html.escape(strip_inline_markdown(heading.group(1))), styles["Heading3"]), Spacer(1, 6)])
        elif line.strip():
            story.extend([Paragraph(html.escape(strip_inline_markdown(line)).replace("\n", "<br/>"), styles["BodyText"]), Spacer(1, 6)])
        else:
            story.append(Spacer(1, 5))
        index += 1
    document.build(story or [Paragraph("No generated content.", styles["BodyText"])])


def create_generated_response_file(user_id: int, conversation_id: int, message_id: int, request_message: str, response_content: str, output_directory: Path | None = None) -> dict[str, str]:
    file_type = select_generated_file_type(request_message)
    file_id = str(uuid.uuid4())
    generated_content = truncate_generated_file_content(response_content, MAX_FILE_CONTENT_SIZE)
    directory = output_directory or Path(GENERATED_FILES_DIRECTORY)
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"biology-response-{file_id[:8]}.{file_type}"
    storage_name = f"{file_id}.{file_type}"
    path = directory / storage_name
    if file_type == "txt":
        path.write_text(generated_content, encoding="utf-8")
        mime_type = "text/plain"
    else:
        create_pdf_response_file(path, generated_content)
        mime_type = "application/pdf"
    return create_generated_file_from_db(file_id, user_id, conversation_id, message_id, filename, mime_type, storage_name)
