import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from hosting.pdf_ingestion import approve, convert_all
from hosting.indexing import markdown_chunks


def fixture_pdf(path, text="Guest towels are in the cupboard.", blank=False):
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    if not blank:
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        content = DecodedStreamObject()
        content.set_data(f"BT /F1 12 Tf 20 250 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(content)
    with path.open("wb") as file:
        writer.write(file)


class PDFTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "source-documents").mkdir()
        (self.root / "docs").mkdir()
        self.pdf = self.root / "source-documents/guide.pdf"
        fixture_pdf(self.pdf)

    def test_real_pdf_converts_to_review_markdown_without_indexing(self):
        result = convert_all(self.root)[0]
        self.assertEqual(result["status"], "review_required")
        text = (self.root / "document-review" / result["markdown"]).read_text()
        self.assertIn("## Page 1", text)
        self.assertIn("Guest towels are in the cupboard.", text)
        with self.assertRaises(ValueError):
            markdown_chunks(self.root)

    def test_separately_positioned_words_form_readable_lines(self):
        writer = PdfWriter()
        page = writer.add_blank_page(width=300, height=300)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 12 Tf 1 0 0 1 20 250 Tm (Pull) Tj 1 0 0 1 48 250 Tm (the) Tj 1 0 0 1 70 250 Tm (cord) Tj 1 0 0 1 20 230 Tm (Press the button.) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(stream)
        with self.pdf.open("wb") as file:
            writer.write(file)
        result = convert_all(self.root)[0]
        text = (self.root / "document-review" / result["markdown"]).read_text()
        self.assertIn("Pull the cord", " ".join(text.split()))
        self.assertIn("Press the button.", text)
        self.assertNotIn("Pull\n", text)

    def test_new_converter_preserves_previous_review_and_approved_text(self):
        with patch("hosting.pdf_ingestion.CONVERSION_VERSION", 1):
            first = convert_all(self.root)[0]
        previous = self.root / "document-review" / first["markdown"]
        previous.write_text("Host reviewed version")
        approved = approve(self.root, first["markdown"])
        second = convert_all(self.root)[0]
        self.assertNotEqual(first["markdown"], second["markdown"])
        self.assertEqual(previous.read_text(), "Host reviewed version")
        self.assertEqual(approved.read_text(), "Host reviewed version\n")
        with self.assertRaises(ValueError):
            approve(self.root, second["markdown"])
        approve(self.root, second["markdown"], replace=True)

    def test_review_edits_are_preserved_and_approved_into_docs(self):
        result = convert_all(self.root)[0]
        review = self.root / "document-review" / result["markdown"]
        review.write_text("# Reviewed guide\n\nOnly guest-safe facts.")
        self.assertEqual(convert_all(self.root)[0]["status"], "existing")
        destination = approve(self.root, result["markdown"])
        self.assertEqual(destination.read_text(), "# Reviewed guide\n\nOnly guest-safe facts.\n")
        self.assertEqual(len(markdown_chunks(self.root)), 1)

    def test_source_change_invalidates_old_review_and_creates_new_version(self):
        first = convert_all(self.root)[0]
        fixture_pdf(self.pdf, text="Updated guide facts.")
        with self.assertRaises(ValueError):
            approve(self.root, first["markdown"])
        second = convert_all(self.root)[0]
        self.assertNotEqual(first["markdown"], second["markdown"])

    def test_existing_approved_markdown_is_not_overwritten_implicitly(self):
        first = convert_all(self.root)[0]
        destination = approve(self.root, first["markdown"])
        destination.write_text("Host-curated approved text")
        with self.assertRaises(ValueError):
            approve(self.root, first["markdown"])
        self.assertEqual(destination.read_text(), "Host-curated approved text")
        approve(self.root, first["markdown"], replace=True)

    def test_image_only_pdf_reports_ocr_requirement(self):
        fixture_pdf(self.pdf, blank=True)
        result = convert_all(self.root)[0]
        self.assertEqual(result["status"], "error")
        self.assertIn("OCR", result["reason"])
        self.assertFalse(list((self.root / "document-review").glob("*.md")))

    def test_partial_extraction_requires_explicit_override(self):
        with patch("hosting.pdf_ingestion.extract_pdf", return_value=("## Page 1\n\nGuest-safe facts", [2])):
            result = convert_all(self.root)[0]
        with self.assertRaises(ValueError):
            approve(self.root, result["markdown"])
        self.assertTrue(approve(self.root, result["markdown"], allow_incomplete=True).exists())

    def test_same_filename_in_different_subfolders_does_not_collide(self):
        other = self.root / "source-documents/subfolder"
        other.mkdir()
        fixture_pdf(other / "guide.pdf")
        results = convert_all(self.root)
        self.assertEqual(len({result["markdown"] for result in results}), 2)

    def test_property_symlink_and_approval_traversal_are_rejected(self):
        (self.root / "source-documents/outside.pdf").symlink_to(self.pdf)
        result = convert_all(self.root)
        self.assertTrue(any(row["status"] == "error" for row in result))
        with self.assertRaises(ValueError):
            approve(self.root, "../another-property.md")

    def test_corrupt_pdf_produces_review_report_without_leaking_contents(self):
        self.pdf.write_bytes(b"private data that is not a PDF")
        result = convert_all(self.root)[0]
        self.assertEqual(result["status"], "error")
        self.assertNotIn("private data", json.dumps(result))

