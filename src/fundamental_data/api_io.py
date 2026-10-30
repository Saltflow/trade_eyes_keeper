"""Read original PDFs and seal inputs; no research parser or network dependency."""

import base64
import hashlib
import importlib.metadata
import io
import json
import sys
from pathlib import Path

import pypdfium2


def write_once(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (
        value
        if isinstance(value, bytes)
        else json.dumps(value, ensure_ascii=False, indent=2).encode()
    )
    with path.open("xb") as stream:
        stream.write(raw)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(source, output):
    manifest = (
        json.loads(source.read_text(encoding="utf-8-sig"))
        if source.is_file()
        else {
            "documents": [
                {"path": str(p.resolve())} for p in sorted(source.rglob("*.pdf"))
            ]
        }
    )
    if not manifest.get("documents"):
        raise ValueError("no_original_pdfs")
    output.mkdir(parents=True, exist_ok=False)
    documents = []
    for spec in manifest["documents"]:
        path = Path(spec["path"])
        path = path if path.is_absolute() else source.parent / path
        digest = sha(path)
        if spec.get("sha256", digest).lower() != digest:
            raise ValueError("original_pdf_hash_mismatch")
        if any(d["sha256"] == digest for d in documents):
            raise ValueError("duplicate_original_pdf")
        doc = {
            **spec,
            "path": str(path.resolve()),
            "sha256": digest,
            "document_id": "D_" + digest[:16],
            "pages": {},
            "images": [],
        }
        with pypdfium2.PdfDocument(str(path)) as pdf:
            for number in range(1, len(pdf) + 1):
                page = pdf[number - 1]
                textpage = page.get_textpage()
                text = textpage.get_text_range()
                textpage.close()
                doc["pages"][str(number)] = text
                if len(text.strip()) < 80:
                    bitmap = page.render(scale=1.7)
                    image = bitmap.to_pil().convert("RGB")
                    image.thumbnail((1600, 1600))
                    stream = io.BytesIO()
                    image.save(stream, format="JPEG", quality=85)
                    doc["images"].append(
                        {
                            "page": number,
                            "base64": base64.b64encode(stream.getvalue()).decode(),
                        }
                    )
                    bitmap.close()
                page.close()
        write_once(output / "sources" / (doc["document_id"] + ".json"), doc)
        documents.append(doc)
    expected = {
        (c, p)
        for c in manifest.get("expected_companies", [])
        for p in manifest.get("expected_periods", [])
    }
    actual = [(d.get("issuer_id"), d.get("period_end")) for d in documents]
    if expected and (set(actual) != expected or len(actual) != len(expected)):
        raise ValueError("input_company_period_matrix_mismatch")
    return manifest, documents


def seal(source, output, documents, settings):
    repo = Path(__file__).resolve().parents[2]
    files = [
        Path(__file__),
        *Path(__file__).parent.glob("api_*.py"),
        repo / "scripts/build_fundamental_data.py",
        repo / "src/fundamental_data/__init__.py",
        repo / "config/fundamental_data_api.md",
        Path(settings["tokenizer"]),
    ]
    files += list((output / "sources").glob("*.json")) + [
        Path(d["path"]) for d in documents
    ]
    if source.is_file():
        files.append(source)
    hashes = {str(p.resolve()): sha(p) for p in files}
    for path in sorted(set(files)):
        if path.suffix in {".py", ".md"}:
            write_once(output / "frozen_code" / path.name, path.read_bytes())
    frozen = {
        "files": hashes,
        "settings": settings,
        "python": sys.version,
        "dependencies": {
            name: importlib.metadata.version(name)
            for name in ("pypdfium2", "requests", "python-dotenv", "tokenizers")
        },
        "rule": "single_attempt_no_retry_no_repair_no_manual_modification",
    }
    write_once(output / "freeze.json", frozen)
    return hashes


def check_seal(hashes):
    if any(sha(Path(path)) != digest for path, digest in hashes.items()):
        raise ValueError("frozen_code_or_input_changed")
