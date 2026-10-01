"""Extraction de texte et réponses extractives."""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from app.llm.extractive import build_extractive_answer
from app.rag.extract import UnsupportedDocument, extract, is_supported
from app.rag.store import SearchHit


def test_formats_reconnus():
    assert is_supported("rapport.pdf")
    assert is_supported("notes.md")
    assert is_supported("tableau.xlsx")
    assert not is_supported("video.mkv")
    assert not is_supported("archive.zip")


def test_texte_brut_utf8():
    segments = extract("notes.txt", "Bonjour le NAS — accents éàü".encode())
    assert "accents éàü" in segments[0].text


def test_texte_brut_latin1():
    segments = extract("notes.txt", "Café crème".encode("cp1252"))
    assert "Caf" in segments[0].text


def test_csv():
    segments = extract("data.csv", b"nom,montant\nAlice,100\nBob,200\n")
    assert "Alice | 100" in segments[0].text


def test_json():
    payload = json.dumps({"cle": "valeur", "liste": [1, 2]}).encode()
    assert "valeur" in extract("data.json", payload)[0].text


def test_html_sans_script():
    html = b"<html><head><title>Titre</title><script>alert(1)</script></head><body><p>Contenu</p></body></html>"
    text = extract("page.html", html)[0].text
    assert "Contenu" in text and "alert" not in text


def test_docx(tmp_path):
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_paragraph("Procédure de sauvegarde")
    buffer = io.BytesIO()
    document.save(buffer)
    assert "Procédure de sauvegarde" in extract("doc.docx", buffer.getvalue())[0].text


def test_format_inconnu():
    with pytest.raises(UnsupportedDocument):
        extract("film.mkv", b"\x00\x01")


def test_fichier_corrompu():
    with pytest.raises(UnsupportedDocument):
        extract("doc.docx", b"ceci n'est pas un docx")


def test_zip_non_docx():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a.txt", "contenu")
    with pytest.raises(UnsupportedDocument):
        extract("doc.pptx", buffer.getvalue())


def hit(text: str, index: int = 1) -> SearchHit:
    return SearchHit(
        chunk_id=index, document_id=index, text=text, ordinal=0, location="page 1",
        real_path="/volume1/documents/a.pdf", dsm_path="/documents/a.pdf",
        share="/documents", name="a.pdf", mtime=0,
    )


def test_reponse_extractive_selectionne_les_phrases_pertinentes():
    hits = [
        hit("La sauvegarde hebdomadaire est lancée chaque dimanche à minuit sur le volume 2."),
        hit("Le menu de la cantine propose un plat végétarien tous les mardis midi.", 2),
    ]
    answer = build_extractive_answer("Quand a lieu la sauvegarde hebdomadaire ?", hits)
    assert "dimanche" in answer
    assert "[1]" in answer


def test_reponse_extractive_sans_extrait():
    assert build_extractive_answer("question", []) == ""


def test_reponse_extractive_repli_sur_le_meilleur_extrait():
    answer = build_extractive_answer("xyzabc", [hit("Un texte sans aucun rapport mais suffisamment long pour être retenu.")])
    assert "Un texte sans aucun rapport" in answer
