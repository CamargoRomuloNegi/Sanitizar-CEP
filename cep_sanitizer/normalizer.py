"""Normalização de CEPs vindos de planilhas.

Regras (todas pensadas para NÃO "inventar" CEPs):

* Espaços nas pontas são ignorados; espaço no meio invalida (o ViaCEP também rejeita).
* Formatos aceitos: ``01001000``, ``01001-000``, ``01.001-000``.
* CEP com menos de 8 dígitos é INVÁLIDO por padrão. Em bases exportadas de sistema, o CEP
  curto costuma estar TRUNCADO (perdeu o último dígito), e completar com zero consultaria
  o CEP errado.
* Para planilhas que passaram pelo Excel (que apaga o zero à esquerda: ``1001000``, ou grava
  ``1001000.0``), use ``pad_leading_zero=True``: com exatamente 7 dígitos completa-se UM zero
  à esquerda (nenhum CEP brasileiro começa com ``00``). Com menos de 7 dígitos segue inválido.
* Letras, 9+ dígitos, ``00000000`` e valores como ``N/A`` são inválidos.
* ``nan``, ``none``, ``null`` e células em branco são tratados como vazios.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Optional

from .models import CEP_INVALIDO, CEP_VAZIO, OK

_EMPTY_TOKENS = frozenset({"", "nan", "none", "null", "nat"})
# "1001000.0" / "13514002,0" (número lido pelo Excel/pandas como decimal)
_FLOAT_LIKE = re.compile(r"^(\d{7,8})[.,]0+$", re.ASCII)
# 8 dígitos (ou 7, com zero perdido), com pontuação opcional
_CEP_FORMAT = re.compile(r"^(\d{1,2})\.?(\d{3})-?(\d{3})$", re.ASCII)


@dataclass(frozen=True)
class NormalizedCep:
    cep: Optional[str]      # 8 dígitos, ou None se inválido/vazio
    status: str             # OK | CEP_VAZIO | CEP_INVALIDO
    padded: bool = False    # True se foi completado com zero à esquerda
    detalhe: str = ""


def normalize_cep(value: Any, pad_leading_zero: bool = False) -> NormalizedCep:
    """Converte o valor de uma célula em um CEP de 8 dígitos (ou explica por que não dá).

    Por padrão o CEP de 7 dígitos é inválido (provavelmente truncado). Com
    ``pad_leading_zero=True`` ele recebe um zero à esquerda, o que só faz sentido quando a
    planilha passou pelo Excel e perdeu o zero inicial.
    """
    if value is None:
        return NormalizedCep(None, CEP_VAZIO, detalhe="CEP em branco")
    if isinstance(value, float):
        if math.isnan(value):
            return NormalizedCep(None, CEP_VAZIO, detalhe="CEP em branco")
        if value.is_integer():
            value = int(value)

    text = str(value).strip()
    if text.casefold() in _EMPTY_TOKENS:
        return NormalizedCep(None, CEP_VAZIO, detalhe="CEP em branco")

    float_like = _FLOAT_LIKE.match(text)
    if float_like:
        text = float_like.group(1)

    match = _CEP_FORMAT.match(text)
    if not match:
        return NormalizedCep(None, CEP_INVALIDO, detalhe=f"formato inválido: {text[:30]!r}")

    digits = "".join(match.groups())
    if set(digits) == {"0"}:
        return NormalizedCep(None, CEP_INVALIDO, detalhe="CEP zerado")

    padded = len(digits) == 7
    if padded:
        if not pad_leading_zero:
            return NormalizedCep(
                None, CEP_INVALIDO,
                detalhe="7 dígitos: possivelmente truncado; zero à esquerda não foi completado",
            )
        digits = "0" + digits
    return NormalizedCep(digits, OK, padded=padded)
