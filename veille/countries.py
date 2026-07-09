"""Normalisation des noms de pays → codes ISO 3166-1 alpha-2.

Les registres AMM émettent déjà des codes ISO ("MA", "NG", "VN"…) mais certaines
sources (ex. inovet, segments d'URL en français) produisent des noms en toutes
lettres ("Maroc", "Viêt Nam", "L Espagne"…). Sans normalisation, un même pays
apparaît en double dans le dashboard et fausse les cartes de couverture.

Usage :
    from veille.countries import to_iso
    to_iso("Maroc")     -> "MA"
    to_iso("MA")        -> "MA"   (déjà ISO, inchangé)
    to_iso("Atlantide") -> "Atlantide"  (inconnu : rendu tel quel, jamais None)
"""
from __future__ import annotations

import re
import unicodedata

# Nom français normalisé (sans accents, minuscules) -> code ISO.
# Couvre les noms produits par inovet + variantes orthographiques usuelles.
_FR_TO_ISO = {
    "afrique du sud": "ZA",
    "albanie": "AL",
    "algerie": "DZ",
    "allemagne": "DE",
    "angola": "AO",
    "arabie saoudite": "SA",
    "armenie": "AM",
    "autriche": "AT",
    "bahrein": "BH",
    "bangladesh": "BD",
    "belgique": "BE",
    "benin": "BJ",
    "birmanie": "MM",
    "bosnie herzegovine": "BA",
    "bulgarie": "BG",
    "burkina faso": "BF",
    "cambodge": "KH",
    "cameroun": "CM",
    "chili": "CL",
    "chine": "CN",
    "chypre": "CY",
    "congo": "CG",
    "coree de nord": "KP",
    "coree du nord": "KP",
    "coree du sud": "KR",
    "costa rica": "CR",
    "cote d ivoire": "CI",
    "croatie": "HR",
    "danemark": "DK",
    "egypte": "EG",
    "emirates arabes unis": "AE",
    "emirats arabes unis": "AE",
    "espagne": "ES",
    "estonie": "EE",
    "ethiopie": "ET",
    "finlande": "FI",
    "france": "FR",
    "gabon": "GA",
    "ghana": "GH",
    "grece": "GR",
    "guinee": "GN",
    "guinee bissau": "GW",
    "hongrie": "HU",
    "inde": "IN",
    "indonesie": "ID",
    "irak": "IQ",
    "iran": "IR",
    "irlande": "IE",
    "israel": "IL",
    "italie": "IT",
    "japon": "JP",
    "jordan": "JO",
    "jordanie": "JO",
    "kenya": "KE",
    "koweit": "KW",
    "liban": "LB",
    "libye": "LY",
    "lituanie": "LT",
    "macedoine du nord": "MK",
    "madagascar": "MG",
    "malaisie": "MY",
    "mali": "ML",
    "malte": "MT",
    "maroc": "MA",
    "maurice": "MU",
    "ile maurice": "MU",
    "mauritanie": "MR",
    "mozambique": "MZ",
    "nepal": "NP",
    "niger": "NE",
    "nigeria": "NG",
    "norvege": "NO",
    "oman": "OM",
    "ouganda": "UG",
    "pakistan": "PK",
    "pays bas": "NL",
    "perou": "PE",
    "philippines": "PH",
    "pologne": "PL",
    "portugal": "PT",
    "qatar": "QA",
    "rd congo": "CD",
    "republique dominicaine": "DO",
    "republique tcheque": "CZ",
    "roumanie": "RO",
    "royaume uni": "GB",
    "russie": "RU",
    "rwanda": "RW",
    "senegal": "SN",
    "serbie": "RS",
    "slovaquie": "SK",
    "slovenie": "SI",
    "soudan": "SD",
    "sri lanka": "LK",
    "suede": "SE",
    "suisse": "CH",
    "suriname": "SR",
    "taiwan": "TW",
    "tanzanie": "TZ",
    "tchad": "TD",
    "thailande": "TH",
    "togo": "TG",
    "tunesie": "TN",   # orthographe rencontrée dans les URLs inovet
    "tunisie": "TN",
    "turquie": "TR",
    "ukraine": "UA",
    "ukrajna": "UA",   # orthographe rencontrée dans les URLs inovet
    "viet nam": "VN",
    "vietnam": "VN",
    "yemen": "YE",
    "zambie": "ZM",
    "zimbabwe": "ZW",
}

_ISO_RE = re.compile(r"^[A-Z]{2}$")


def _norm(name: str) -> str:
    """'Côte D Ivoire' → 'cote d ivoire' (sans accents, espaces réduits)."""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[\s\-']+", " ", s).strip().lower()
    # Article résiduel des segments d'URL inovet : "l autriche" → "autriche"
    if s.startswith("l "):
        s = s[2:]
    return s


def to_iso(name: str | None) -> str | None:
    """Convertit un nom de pays en code ISO alpha-2 ; rend l'entrée telle quelle
    si déjà ISO ou inconnue (jamais None pour une entrée non vide)."""
    if not name:
        return name
    raw = name.strip()
    if _ISO_RE.match(raw):
        return raw
    return _FR_TO_ISO.get(_norm(raw), raw)
