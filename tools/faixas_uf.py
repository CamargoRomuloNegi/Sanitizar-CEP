"""Faixas de CEP por UF (heurística de plausibilidade).

Usada apenas para gerar CEPs de teste e para sinalizar resultados suspeitos
(ex.: um CEP 01xxxxxx devolvendo UF "RS"). Fronteiras podem divergir em casos
raros; por isso um desvio gera "REVISAR", nunca "FAIL".
"""

from __future__ import annotations

# (UF, CEP inicial, CEP final) - 8 dígitos, comparação lexicográfica funciona.
FAIXAS_UF: list[tuple[str, str, str]] = [
    ("SP", "01000000", "19999999"),
    ("RJ", "20000000", "28999999"),
    ("ES", "29000000", "29999999"),
    ("MG", "30000000", "39999999"),
    ("BA", "40000000", "48999999"),
    ("SE", "49000000", "49999999"),
    ("PE", "50000000", "56999999"),
    ("AL", "57000000", "57999999"),
    ("PB", "58000000", "58999999"),
    ("RN", "59000000", "59999999"),
    ("CE", "60000000", "63999999"),
    ("PI", "64000000", "64999999"),
    ("MA", "65000000", "65999999"),
    ("PA", "66000000", "68899999"),
    ("AP", "68900000", "68999999"),
    ("AM", "69000000", "69299999"),
    ("RR", "69300000", "69399999"),
    ("AM", "69400000", "69899999"),
    ("AC", "69900000", "69999999"),
    ("DF", "70000000", "72799999"),
    ("GO", "72800000", "72999999"),
    ("DF", "73000000", "73699999"),
    ("GO", "73700000", "76799999"),
    ("RO", "76800000", "76999999"),
    ("TO", "77000000", "77999999"),
    ("MT", "78000000", "78899999"),
    ("MS", "79000000", "79999999"),
    ("PR", "80000000", "87999999"),
    ("SC", "88000000", "89999999"),
    ("RS", "90000000", "99999999"),
]


def ufs_esperadas(cep8: str) -> set[str]:
    """UFs cuja faixa contém o CEP (vazio se o CEP estiver fora de todas as faixas)."""
    return {uf for uf, ini, fim in FAIXAS_UF if ini <= cep8 <= fim}
