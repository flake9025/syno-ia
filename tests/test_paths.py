"""Correspondance chemins réels ↔ chemins DSM."""

from __future__ import annotations

from app.synology.models import ShareInfo
from app.synology.paths import PathMapper, normalize


def mapper() -> PathMapper:
    return PathMapper.from_shares(
        [
            ShareInfo(path="/documents", name="documents", real_path="/volume1/documents"),
            ShareInfo(path="/rh", name="rh", real_path="/volume2/rh-prive"),
            ShareInfo(path="/photo", name="photo", real_path="/volume1/photo"),
        ]
    )


def test_normalize():
    assert normalize("/volume1//documents/") == "/volume1/documents"
    assert normalize("C:\\x\\y".replace("C:", "")) == "/x/y"
    assert normalize("") == ""


def test_conversion_vers_chemin_dsm():
    converted = mapper().to_dsm("/volume1/documents/rh/contrat.pdf")
    assert converted == "/documents/rh/contrat.pdf"


def test_conversion_avec_volume_different():
    assert mapper().to_dsm("/volume2/rh-prive/salaires.xlsx") == "/rh/salaires.xlsx"


def test_repli_sans_cartographie():
    """Sans compte de service, le préfixe de volume est simplement retiré."""
    assert PathMapper.empty().to_dsm("/volume1/documents/a.pdf") == "/documents/a.pdf"
    assert PathMapper.empty().to_dsm("/volumeUSB1/usbshare/a.pdf") == "/usbshare/a.pdf"


def test_conversion_inverse():
    assert mapper().to_real("/documents/rh/contrat.pdf") == "/volume1/documents/rh/contrat.pdf"


def test_partage_racine():
    assert PathMapper.share_of("/documents/rh/contrat.pdf") == "/documents"
    assert PathMapper.share_of("/documents") == "/documents"
    assert PathMapper.share_of("/") == ""


def test_le_prefixe_le_plus_long_gagne():
    specific = PathMapper.from_shares(
        [
            ShareInfo(path="/data", name="data", real_path="/volume1/data"),
            ShareInfo(path="/data-rh", name="data-rh", real_path="/volume1/data-rh"),
        ]
    )
    # « /volume1/data-rh » ne doit pas être capté par le partage « /volume1/data ».
    assert specific.to_dsm("/volume1/data-rh/x.pdf") == "/data-rh/x.pdf"
    assert specific.to_dsm("/volume1/data/x.pdf") == "/data/x.pdf"
