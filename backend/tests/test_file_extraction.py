import unittest

from fastapi import HTTPException

from app.services.file_extraction_service import convert_pdf_table_to_markdown, extract_text_from_uploaded_file


class FileExtractionTests(unittest.TestCase):
    def test_extracts_utf8_text_file(self):
        result = extract_text_from_uploaded_file("notes.txt", "text/plain", b"Viruses require host cells to replicate.")
        self.assertEqual(result, "Viruses require host cells to replicate.")

    def test_converts_pdf_table_to_markdown(self):
        result = convert_pdf_table_to_markdown([["Name", "Type"], ["E. coli", "Bacterium"]])
        self.assertEqual(result, "| Name | Type |\n| --- | --- |\n| E. coli | Bacterium |")

    def test_rejects_unsupported_file_type(self):
        with self.assertRaises(HTTPException) as raised:
            extract_text_from_uploaded_file("image.png", "image/png", b"not a real image")
        self.assertEqual(raised.exception.status_code, 415)
