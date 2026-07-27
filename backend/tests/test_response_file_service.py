import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

import pdfplumber

from app.services.response_file_service import create_generated_response_file


class ResponseFileServiceTests(unittest.TestCase):
    @patch("app.services.response_file_service.create_generated_file_from_db")
    def test_creates_pdf_with_markdown_table_by_default(self, create_file_record):
        create_file_record.side_effect = lambda file_id, _user_id, _conversation_id, _message_id, filename, mime_type, storage_name: {"id": file_id, "filename": filename, "mime_type": mime_type, "storage_name": storage_name}
        with tempfile.TemporaryDirectory() as directory:
            generated_file = create_generated_response_file(1, 1, 1, "Create a comparison", "| Feature | Bacteria | Virus |\n| --- | --- | --- |\n| Cells | Yes | No |", Path(directory))
            path = Path(directory) / generated_file["storage_name"]
            self.assertEqual(generated_file["mime_type"], "application/pdf")
            with pdfplumber.open(path) as document:
                extracted_text = "\n".join(page.extract_text() or "" for page in document.pages)
            self.assertIn("Bacteria", extracted_text)
            self.assertIn("Virus", extracted_text)

    @patch("app.services.response_file_service.create_generated_file_from_db")
    def test_creates_txt_when_requested(self, create_file_record):
        create_file_record.side_effect = lambda file_id, _user_id, _conversation_id, _message_id, filename, mime_type, storage_name: {"id": file_id, "filename": filename, "mime_type": mime_type, "storage_name": storage_name}
        with tempfile.TemporaryDirectory() as directory:
            generated_file = create_generated_response_file(1, 1, 1, "Give me a TXT file", "Bacteria are cellular organisms.", Path(directory))
            path = Path(directory) / generated_file["storage_name"]
            self.assertEqual(generated_file["mime_type"], "text/plain")
            self.assertEqual(path.read_text(encoding="utf-8"), "Bacteria are cellular organisms.")
