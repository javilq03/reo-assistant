import base64
import io
import zipfile

import pytest

from reo import docs


def zipped(files: dict) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    return b.getvalue()


W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
DOCX = zipped({"word/document.xml": f'<w:document {W}><w:body><w:p><w:r><w:t>Hello</w:t></w:r><w:r><w:t xml:space="preserve"> world</w:t></w:r></w:p>'
               f'<w:p><w:r><w:t>Second</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>A1</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>B1</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>'})
A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
PPTX = zipped({"ppt/slides/slide2.xml": f'<p:sld xmlns:p="p" {A}><a:p><a:r><a:t>Second slide</a:t></a:r></a:p></p:sld>',
               "ppt/slides/slide1.xml": f'<p:sld xmlns:p="p" {A}><a:p><a:r><a:t>Title</a:t></a:r></a:p><a:p><a:r><a:t>Bullet</a:t></a:r></a:p></p:sld>'})
S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
XLSX = zipped({"xl/sharedStrings.xml": f'<sst {S}><si><t>Name</t></si><si><t>Arche</t></si></sst>',
               "xl/workbook.xml": f'<workbook {S} {R}><sheets><sheet name="Money" sheetId="1" r:id="rId1"/></sheets></workbook>',
               "xl/_rels/workbook.xml.rels": '<Relationships xmlns="r"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
               "xl/worksheets/sheet1.xml": f'<worksheet {S}><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="C1"><v>5</v></c></row>'
                                           f'<row r="2"><c r="A2" t="s"><v>1</v></c><c r="B2"><v>250</v></c></row></sheetData></worksheet>'})
ODT = zipped({"content.xml": '<office:document-content xmlns:office="o" xmlns:text="t"><text:h>Heading</text:h><text:p>Body <text:span>text</text:span></text:p></office:document-content>'})


def make_pdf(text: str) -> bytes:
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    stream = f"BT /F1 18 Tf 20 100 Td ({text}) Tj ET"
    objs[3] = f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out)); out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    x = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offs)
    return out + f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{x}\n%%EOF".encode()


def test_word_excel_powerpoint_opendocument_rtf():
    t = docs.extract("a.docx", DOCX)["text"]
    assert "Hello world" in t and "Second" in t and "A1 | B1" in t
    p = docs.extract("a.pptx", PPTX)
    assert p["pages"] == 2 and p["text"].index("Slide 1") < p["text"].index("Second slide") and "Bullet" in p["text"]
    x = docs.extract("a.xlsx", XLSX)["text"]
    assert "Sheet: Money" in x and "Name |  | 5" in x and "Arche | 250" in x
    assert "Heading" in docs.extract("a.odt", ODT)["text"] and "Body text" in docs.extract("a.odt", ODT)["text"]
    assert docs.extract("a.rtf", rb"{\rtf1\ansi Hello \b bold\b0  caf\'e9\par Next line}")["text"].startswith("Hello bold") and "café" in docs.extract("a.rtf", rb"{\rtf1 caf\'e9}")["text"]


def test_pdf_text_and_scanned_pdf():
    pytest.importorskip("pypdf")
    r = docs.extract("a.pdf", make_pdf("Hineloim ledger"))
    assert "Hineloim ledger" in r["text"] and r["pages"] == 1
    with pytest.raises(docs.DocError, match="no selectable text"):
        docs.extract("scan.pdf", make_pdf(""))
    with pytest.raises(docs.DocError):
        docs.extract("bad.pdf", b"not a pdf at all")


def test_text_csv_json_html_and_refusals():
    assert docs.extract("n.csv", b"a,b\n1,2\n")["text"] == "a | b\n1 | 2"
    assert '"x": 1' in docs.extract("n.json", b'{"x":1}')["text"]
    assert "Hi there" in docs.extract("n.html", b"<html><body><script>x</script><p>Hi there</p></body></html>")["text"]
    assert docs.extract("n.txt", "café".encode("cp1252"))["text"] == "café"
    for name, data, msg in [("old.doc", b"x", "old binary"), ("x.docx", b"junk", "damaged"), ("b.bin", b"\x00\x01\x02" * 50, "binary"), ("e.txt", b"  ", "No text")]:
        with pytest.raises(docs.DocError, match=msg):
            docs.extract(name, data)


def test_long_documents_are_cut_with_a_notice():
    r = docs.extract("big.txt", ("word " * 5000).encode(), max_chars=1000)
    assert r["truncated"] and r["chars"] == 24999 and "first 1,000 of 24,999" in r["text"]


def test_bridge_read_document_and_taint(home):
    from reo.bridge import Bridge
    b = Bridge()
    ok = b.read_document("a.docx", base64.b64encode(DOCX).decode())
    assert ok["ok"] and "Hello world" in ok["text"]
    assert not b.read_document("a.docx", base64.b64encode(b"junk").decode())["ok"]
