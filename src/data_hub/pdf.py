"""The neutral PDF backend; finance prompts and execution selections stay out."""

from __future__ import annotations

import base64
import io


def read_pdf_material(path, *, render_images=True, max_pages=None, max_chars=None):
    import pypdfium2

    content, pages, images = [], [], []
    characters = 0
    with pypdfium2.PdfDocument(str(path)) as pdf:
        if max_pages is not None and len(pdf) > max_pages:
            raise ValueError(f"pdf_page_limit_exceeded:{max_pages}")
        for number, page in enumerate(pdf, 1):
            try:
                textpage = page.get_textpage()
                try:
                    if max_chars is not None:
                        characters += textpage.count_chars()
                        if characters > max_chars:
                            raise ValueError(f"pdf_text_limit_exceeded:{max_chars}")
                    text = textpage.get_text_range()
                    pages.append(text)
                finally:
                    textpage.close()
                if len(text.strip()) < 80:
                    if not render_images:
                        # Record visual coverage without rendering or OCR. The
                        # default research path still preserves page images.
                        images.append({"page": number})
                        continue
                    bitmap = page.render(scale=1.7)
                    try:
                        picture = bitmap.to_pil().convert("RGB")
                        picture.thumbnail((1600, 1600))
                        buffer = io.BytesIO()
                        picture.save(buffer, format="JPEG", quality=85)
                        encoded = base64.b64encode(buffer.getvalue()).decode()
                        content.append(
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "data:image/jpeg;base64," + encoded
                                },
                            }
                        )
                        images.append({"page": number, "base64": encoded})
                    finally:
                        bitmap.close()
            finally:
                page.close()
    content.insert(0, {"type": "text", "text": "\n\n".join(pages)})
    return {
        "content": content,
        "pages": len(pages),
        "page_texts": {str(i): text for i, text in enumerate(pages, 1)},
        "images": images,
    }


class PDFParts(tuple):
    """The old two-value backend result, with lossless neutral page metadata."""

    def __new__(cls, material):
        value = super().__new__(cls, (material["content"], material["pages"]))
        value.material = material
        return value


def read_pdf(path):
    return PDFParts(read_pdf_material(path))


def render_pages(path, numbers, *, dpi=120):
    import pypdfium2

    output = []
    with pypdfium2.PdfDocument(str(path)) as pdf:
        for number in numbers:
            page = pdf[number - 1]
            try:
                bitmap = page.render(scale=dpi / 72)
                try:
                    image = bitmap.to_pil().convert("RGB")
                    image.thumbnail((1600, 1600))
                    stream = io.BytesIO()
                    image.save(stream, format="JPEG", quality=85)
                    output.append(
                        {
                            "page": number,
                            "base64": base64.b64encode(stream.getvalue()).decode(),
                        }
                    )
                finally:
                    bitmap.close()
            finally:
                page.close()
    return output
