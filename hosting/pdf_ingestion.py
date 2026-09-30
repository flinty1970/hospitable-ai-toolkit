"""Property-scoped PDF text extraction and explicit guest-knowledge approval."""
import argparse
import hashlib
import json
import os
import re
import time
from pathlib import Path


class PDFProblem(ValueError):
    pass


def confined_folder(root, name):
    root = Path(root).resolve()
    path = root / name
    if path.is_symlink() or root not in path.resolve().parents:
        raise ValueError("Document folder escapes selected property")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def write_atomic(path, text):
    import tempfile
    fd, temporary = tempfile.mkstemp(prefix=".conversion-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def extract_pdf(path):
    from pypdf import PdfReader
    import logging
    # Parser warnings may include bytes from private documents.
    logger = logging.getLogger("pypdf")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    if path.stat().st_size > 50 * 1024 * 1024:
        raise PDFProblem("PDF exceeds the 50 MB limit")
    reader = PdfReader(path)
    if reader.is_encrypted and not reader.decrypt(""):
        raise PDFProblem("Password-protected PDF needs an unlocked source copy")
    if len(reader.pages) > 1000:
        raise PDFProblem("PDF exceeds the 1000-page limit")
    pages, blank_pages, characters = [], [], 0
    for number, page in enumerate(reader.pages, 1):
        text = (page.extract_text() or "").replace("\x00", "").strip()
        characters += len(text)
        if characters > 2_000_000:
            raise PDFProblem("Extracted text exceeds the 2 million character limit")
        if not text:
            blank_pages.append(number)
        else:
            pages.append(f"## Page {number}\n\n{text}\n")
    if not pages:
        raise PDFProblem("No extractable text; scanned PDF requires OCR")
    return "\n".join(pages), blank_pages


def convert_all(root):
    source = confined_folder(root, "source-documents")
    review = confined_folder(root, "document-review")
    results = []
    for path in sorted(source.rglob("*")):
        if not path.is_file() or path.suffix.lower() != ".pdf":
            continue
        if path.is_symlink() or source.resolve() not in path.resolve().parents:
            results.append({"status": "error", "reason": "Source PDF escapes property"})
            continue
        relative = str(path.relative_to(source))
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.stat().st_size <= 50 * 1024 * 1024 else None
        if digest is None:
            results.append({"source": relative, "status": "error", "reason": "PDF exceeds the 50 MB limit"})
            continue
        source_id = hashlib.sha256(relative.encode()).hexdigest()[:12]
        slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", path.stem)[:60] or "document"
        filename = f"{slug}-{source_id}-{digest[:16]}.md"
        markdown = review / filename
        metadata = markdown.with_suffix(".json")
        if markdown.exists() and metadata.exists():
            results.append({"source": relative, "status": "existing", "markdown": filename})
            continue  # Preserve edits made during human review.
        try:
            text, blank_pages = extract_pdf(path)
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise PDFProblem("Source PDF changed during extraction; retry")
            write_atomic(markdown, f"# {path.stem}\n\n{text}")
            write_atomic(metadata, json.dumps({"schema": 1, "source": relative,
                "source_sha256": digest, "source_id": source_id,
                "blank_pages": blank_pages, "converted_at": time.time()}, indent=2))
            results.append({"source": relative, "status": "review_required", "markdown": filename, "blank_pages": blank_pages})
        except Exception as error:
            # Do not leak PDF contents, passwords or parser exception messages.
            reason = str(error) if isinstance(error, PDFProblem) else "PDF extraction failed; inspect source locally"
            results.append({"source": relative, "status": "error", "reason": reason})
    write_atomic(review / "conversion-report.json", json.dumps(results, indent=2))
    return results


def approve(root, filename, replace=False, allow_incomplete=False):
    if Path(filename).name != filename or not filename.endswith(".md"):
        raise ValueError("Select a converted Markdown filename")
    review = confined_folder(root, "document-review")
    markdown = review / filename
    metadata = markdown.with_suffix(".json")
    if markdown.is_symlink() or metadata.is_symlink():
        raise ValueError("Review files must not be symlinks")
    record = json.loads(metadata.read_text())
    source_root = confined_folder(root, "source-documents")
    source = source_root / record["source"]
    if source.is_symlink() or source_root.resolve() not in source.resolve().parents:
        raise ValueError("Source PDF escapes property")
    if hashlib.sha256(source.read_bytes()).hexdigest() != record["source_sha256"]:
        raise ValueError("Source PDF changed; reconvert and review the new version")
    if record.get("blank_pages") and not allow_incomplete:
        raise ValueError("Some pages have no extracted text; inspect/OCR them before approval")
    text = markdown.read_text().strip()
    if not text:
        raise ValueError("Approved Markdown must not be empty")
    docs = confined_folder(root, "docs")
    destination = docs / f"pdf-{record['source_id']}.md"
    if destination.is_symlink() or destination.resolve().parent != docs.resolve():
        raise ValueError("Approved file escapes property")
    if destination.exists() and not replace:
        raise ValueError("Approved document exists; use --replace after reviewing changes")
    write_atomic(destination, text + "\n")
    record.update({"approved_at": time.time(), "approved_sha256": hashlib.sha256(text.encode()).hexdigest(),
                   "approved_document": destination.name, "allow_incomplete": allow_incomplete})
    write_atomic(metadata, json.dumps(record, indent=2))
    return destination


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["convert", "approve"])
    parser.add_argument("filename", nargs="?")
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--allow-incomplete", action="store_true")
    args = parser.parse_args()
    root = os.environ["TOOLKIT_PROPERTY_DIR"]
    if args.action == "convert":
        print(json.dumps(convert_all(root), indent=2))
    else:
        if not args.filename:
            parser.error("approve requires a reviewed Markdown filename")
        print(approve(root, args.filename, args.replace, args.allow_incomplete).name)


if __name__ == "__main__":
    main()
