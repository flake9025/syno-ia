"""Découpage du texte."""

from __future__ import annotations

from app.rag.chunker import TextSegment, chunk_segments, chunk_text, clean_text


def test_clean_text_normalise_les_espaces_et_les_cesures():
    raw = "Une phrase  avec\tdes   espaces\net un mot cou-\npé.\n\n\n\nFin."
    cleaned = clean_text(raw)
    assert "coupé" in cleaned
    assert "\n\n\n" not in cleaned
    assert "  " not in cleaned


def test_chunk_respecte_la_taille_maximale():
    text = "Phrase de test numéro 0. " + " ".join(f"Phrase numéro {i}." for i in range(400))
    chunks = chunk_text(text, chunk_size=300, overlap=50)
    assert chunks, "le découpage doit produire des fragments"
    # Le recouvrement s'ajoute en tête : on tolère la taille du recouvrement.
    assert all(len(chunk.text) <= 300 + 50 + 2 for chunk in chunks)


def test_chunk_applique_un_recouvrement():
    text = ". ".join(f"Segment numéro {i} avec suffisamment de texte" for i in range(60))
    chunks = chunk_text(text, chunk_size=280, overlap=80)
    assert len(chunks) > 2
    # Le début du deuxième fragment doit reprendre de la matière du premier.
    assert any(word in chunks[0].text for word in chunks[1].text.split()[:5])


def test_chunk_preserve_la_localisation():
    segments = [
        TextSegment(text="Contenu de la première page. " * 20, location="page 1"),
        TextSegment(text="Contenu de la seconde page. " * 20, location="page 2"),
    ]
    chunks = chunk_segments(segments, chunk_size=200, overlap=20)
    locations = {chunk.location for chunk in chunks}
    assert locations == {"page 1", "page 2"}


def test_chunk_ignore_le_vide():
    assert chunk_text("   \n\n  ", chunk_size=200) == []


def test_mot_unique_tres_long_est_decoupe():
    chunks = chunk_text("a" * 1000, chunk_size=250, overlap=0)
    assert len(chunks) == 4
