"""Bounded, stateless document exports for the browser-local LMC AI workspace."""

from __future__ import annotations

from html import escape
from io import BytesIO
import re
from zipfile import ZIP_DEFLATED, ZipFile

from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas


_PDF_FONT = "MSung-Light"
_SAFE_FILENAME_RE = re.compile(r"[^\w\u3400-\u9fff -]+", re.UNICODE)


def _xml_10_text(value: str) -> str:
    return "".join(
        character
        for character in str(value or "")
        if character in "\t\n\r"
        or 0x20 <= ord(character) <= 0xD7FF
        or 0xE000 <= ord(character) <= 0xFFFD
        or 0x10000 <= ord(character) <= 0x10FFFF
    )


def safe_export_stem(title: str) -> str:
    stem = _SAFE_FILENAME_RE.sub("", str(title or "")).strip(" ._-")
    return stem[:80] or "自家AI文件"


def build_markdown_export(title: str, content: str) -> bytes:
    body = str(content or "")
    if body.lstrip().startswith("#"):
        rendered = body
    else:
        rendered = f"# {str(title or '自家AI文件').strip()}\n\n{body}"
    return ("\ufeff" + rendered.rstrip() + "\n").encode("utf-8")


def _pdf_lines(text: str, font_name: str, font_size: float, max_width: float):
    for source_line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not source_line:
            yield ""
            continue
        line = ""
        width = 0.0
        for character in source_line:
            character_width = pdfmetrics.stringWidth(character, font_name, font_size)
            if line and width + character_width > max_width:
                yield line
                line = character
                width = character_width
            else:
                line += character
                width += character_width
        yield line


def build_pdf_export(title: str, content: str) -> bytes:
    if _PDF_FONT not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(_PDF_FONT))
    buffer = BytesIO()
    page_width, page_height = A4
    margin = 52
    document = canvas.Canvas(buffer, pagesize=A4, pageCompression=1)
    document.setTitle(str(title or "自家AI文件"))

    def start_page():
        document.setFont(_PDF_FONT, 10.5)
        return page_height - margin

    y = start_page()
    document.setFont(_PDF_FONT, 18)
    for line in _pdf_lines(str(title or "自家AI文件"), _PDF_FONT, 18, page_width - 2 * margin):
        document.drawString(margin, y, line)
        y -= 25
    y -= 8
    document.setFont(_PDF_FONT, 10.5)
    for line in _pdf_lines(content, _PDF_FONT, 10.5, page_width - 2 * margin):
        if y < margin:
            document.showPage()
            y = start_page()
        document.drawString(margin, y, line)
        y -= 16
    document.showPage()
    document.save()
    return buffer.getvalue()


def _docx_paragraph(line: str, *, title: bool = False) -> str:
    style = '<w:pStyle w:val="Title"/>' if title else ""
    text = escape(_xml_10_text(line), quote=False)
    return (
        f"<w:p><w:pPr>{style}</w:pPr><w:r>"
        f'<w:t xml:space="preserve">{text}</w:t></w:r></w:p>'
    )


def build_docx_export(title: str, content: str) -> bytes:
    paragraphs = [_docx_paragraph(str(title or "自家AI文件"), title=True)]
    paragraphs.extend(
        _docx_paragraph(line)
        for line in str(content or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    )
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{''.join(paragraphs)}"
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>'
        "</w:body></w:document>"
    )
    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
        '<w:name w:val="Normal"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="Title">'
        '<w:name w:val="Title"/><w:basedOn w:val="Normal"/>'
        '<w:rPr><w:b/><w:sz w:val="36"/></w:rPr></w:style></w:styles>'
    )
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
            "</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            "</Relationships>",
        )
        archive.writestr("word/document.xml", document_xml)
        archive.writestr("word/styles.xml", styles_xml)
        archive.writestr(
            "word/_rels/document.xml.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            "</Relationships>",
        )
    return output.getvalue()
