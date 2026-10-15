"""Confere a planilha de saída contra o gabarito da massa de teste.

Uso:
    python -m cep_sanitizer dados_exemplo/massa_teste_1000.csv --sem-cabecalho \\
        --filtro-coluna Usado --filtro-maior-que 0 -o saida_teste.csv
    python tools/validar_resultado.py --saida saida_teste.csv \\
        --gabarito dados_exemplo/massa_teste_1000_gabarito.csv

Níveis:
    FAIL     o app violou uma regra (ou um gabarito determinístico/verificado divergiu)
    REVISAR  provável divergência de dado externo (gabarito "provável", UF fora da faixa
             do CEP, erro transitório, linha pendente): exige olhar humano
    PASS     tudo conforme

Além do gabarito, o validador confere regras que valem para qualquer linha:
    * o CEP original não foi alterado;
    * diagnóstico OK => cidade/UF preenchidas e Status "OK Cidade e UF" ou "Rever - CIDADE ou UF",
      e o Status confere com uma nova comparação entre a base e o retorno;
    * CEP em branco/inválido/inexistente/com erro => "não encontrado" e Status "Não Encontrado";
    * linha fora do filtro ou pendente => cidade, UF e Status em branco.

Código de saída: 1 se houver algum FAIL; 0 caso contrário.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cep_sanitizer.cli import COLUNAS_PADRAO  # noqa: E402
from cep_sanitizer.io_utils import read_table  # noqa: E402
from cep_sanitizer.models import (  # noqa: E402
    NAO_ENCONTRADO,
    STATUS_NAO_ENCONTRADO,
    STATUS_OK,
    STATUS_REVER,
)
from cep_sanitizer.normalizer import normalize_cep  # noqa: E402
from cep_sanitizer.processor import (  # noqa: E402
    COL_CIDADE,
    COL_DIAG,
    COL_STATUS,
    COL_UF,
    _norm_place,
    detect_place_columns,
)
from tools.faixas_uf import ufs_esperadas  # noqa: E402

SEM_RESPOSTA = {"NAO_EXISTE", "CEP_INVALIDO", "CEP_VAZIO"}
CODIGOS_CONHECIDOS = SEM_RESPOSTA | {"OK", "FORA_DO_FILTRO", "PENDENTE"}
_ORDEM = {"PASS": 0, "REVISAR": 1, "FAIL": 2}


@dataclass
class Relatorio:
    achados: list = field(default_factory=list)  # (linha, cenario, nivel, motivo)
    por_nivel: Counter = field(default_factory=Counter)
    por_cenario: dict = field(default_factory=dict)

    @property
    def falhou(self) -> bool:
        return self.por_nivel.get("FAIL", 0) > 0


def _codigo(diag: str) -> str:
    return str(diag).split(" - ")[0].split(" | ")[0].strip()


def validar(
    saida: pd.DataFrame,
    gabarito: pd.DataFrame,
    coluna_cep: str = "CEP",
    coluna_cidade: Optional[str] = None,
    coluna_uf: Optional[str] = None,
) -> Relatorio:
    if len(saida) != len(gabarito):
        raise ValueError(f"Saída tem {len(saida)} linhas e o gabarito {len(gabarito)}.")
    for col in (coluna_cep, COL_CIDADE, COL_UF, COL_DIAG):
        if col not in saida.columns:
            raise ValueError(f"Coluna ausente na saída: {col!r}")
    tem_status = COL_STATUS in saida.columns
    if not tem_status and (gabarito["esperado_status"] != "*").any():
        raise ValueError(f"Coluna ausente na saída: {COL_STATUS!r} (rode sem --sem-comparacao)")

    auto_cidade, auto_uf = detect_place_columns(saida.columns)
    coluna_cidade = coluna_cidade or auto_cidade
    coluna_uf = coluna_uf or auto_uf
    recomparar = tem_status and coluna_cidade in saida.columns and coluna_uf in saida.columns
    base_cidade = saida[coluna_cidade].astype(str).tolist() if recomparar else [""] * len(saida)
    base_uf = saida[coluna_uf].astype(str).tolist() if recomparar else [""] * len(saida)
    status_col = saida[COL_STATUS].astype(str).tolist() if tem_status else [""] * len(saida)

    rel = Relatorio()
    linhas = zip(
        gabarito["linha"], gabarito["cenario"], gabarito["confianca"], gabarito["cep_original"],
        gabarito["esperado_diag"], gabarito["esperado_status"], gabarito["esperado_cidade"],
        gabarito["esperado_uf"], saida[coluna_cep].astype(str), saida[COL_CIDADE].astype(str),
        saida[COL_UF].astype(str), saida[COL_DIAG].astype(str), status_col, base_cidade, base_uf,
    )
    for (linha, cenario, conf, cep_orig, e_diag, e_status, e_cid, e_uf,
         cep, cidade, uf, diag, status, b_cidade, b_uf) in linhas:
        achados: list[tuple[str, str]] = []
        nivel_div = "FAIL" if conf in ("deterministico", "verificado") else "REVISAR"
        codigo = _codigo(diag)

        if cep != cep_orig:
            achados.append(("FAIL", f"CEP original foi alterado ({cep_orig!r} -> {cep!r})"))

        if e_diag != "*" and codigo != e_diag:
            achados.append((nivel_div, f"diagnóstico {codigo} != esperado {e_diag}"))
        if tem_status and e_status != "*" and status != e_status:
            achados.append((nivel_div, f"Status {status!r} != esperado {e_status!r}"))
        if e_cid != "*" and cidade.casefold() != e_cid.casefold():
            achados.append((nivel_div, f"cidade {cidade!r} != esperada {e_cid!r}"))
        if e_uf != "*" and uf.casefold() != e_uf.casefold():
            achados.append((nivel_div, f"UF {uf!r} != esperada {e_uf!r}"))

        # Regras do app (valem para qualquer linha, com ou sem gabarito)
        if codigo == "OK":
            if not cidade or not uf or NAO_ENCONTRADO in (cidade, uf):
                achados.append(("FAIL", "diagnóstico OK sem cidade/UF preenchidas"))
            if tem_status:
                if status not in (STATUS_OK, STATUS_REVER):
                    achados.append(("FAIL", f"diagnóstico OK com Status inválido: {status!r}"))
                elif recomparar:
                    confere = (
                        _norm_place(b_cidade) != ""
                        and _norm_place(b_cidade) == _norm_place(cidade)
                        and b_uf.strip().upper() == uf.strip().upper()
                    )
                    esperado = STATUS_OK if confere else STATUS_REVER
                    if status != esperado:
                        achados.append(("FAIL", f"Status {status!r} não confere com a comparação (esperado {esperado!r})"))
            consultado = normalize_cep(cep).cep
            esperadas = ufs_esperadas(consultado) if consultado else set()
            if esperadas and uf.upper() not in esperadas:
                achados.append(("REVISAR", f"UF {uf} fora da faixa esperada {sorted(esperadas)} para o CEP"))
        elif codigo in SEM_RESPOSTA or codigo.startswith("ERRO_"):
            if cidade != NAO_ENCONTRADO or uf != NAO_ENCONTRADO:
                achados.append(("FAIL", f'{codigo} deveria gravar "{NAO_ENCONTRADO}" em cidade e UF'))
            if tem_status and status != STATUS_NAO_ENCONTRADO:
                achados.append(("FAIL", f'{codigo} deveria ter Status "{STATUS_NAO_ENCONTRADO}" (veio {status!r})'))
            if codigo.startswith("ERRO_"):
                achados.append(("REVISAR", "erro transitório: rode de novo para reprocessar"))
        elif codigo in ("FORA_DO_FILTRO", "PENDENTE"):
            if cidade != "" or uf != "" or (tem_status and status != ""):
                achados.append(("FAIL", f"{codigo} deveria deixar cidade, UF e Status em branco"))
            if codigo == "PENDENTE":
                achados.append(("REVISAR", "linha pendente: execução incompleta"))
        else:
            achados.append(("FAIL", f"código de diagnóstico desconhecido: {codigo!r}"))

        nivel = max((n for n, _ in achados), key=_ORDEM.get, default="PASS")
        rel.por_nivel[nivel] += 1
        rel.por_cenario.setdefault(cenario, Counter())[nivel] += 1
        for n, motivo in achados:
            rel.achados.append((linha, cenario, n, motivo))
    return rel


def imprimir(rel: Relatorio, limite: int = 15) -> None:
    total = sum(rel.por_nivel.values())
    print(f"\nLinhas conferidas: {total}")
    print(f"  PASS={rel.por_nivel.get('PASS', 0)}  REVISAR={rel.por_nivel.get('REVISAR', 0)}  FAIL={rel.por_nivel.get('FAIL', 0)}")
    print("\nPor cenário:")
    for cen in sorted(rel.por_cenario):
        c = rel.por_cenario[cen]
        print(f"  {cen:<26} PASS={c.get('PASS', 0):<4} REVISAR={c.get('REVISAR', 0):<4} FAIL={c.get('FAIL', 0)}")
    for nivel in ("FAIL", "REVISAR"):
        itens = [a for a in rel.achados if a[2] == nivel]
        if itens:
            print(f"\n{nivel} (primeiros {min(limite, len(itens))} de {len(itens)}):")
            for linha, cen, _, motivo in itens[:limite]:
                print(f"  linha {linha} [{cen}] {motivo}")
    print("\nResultado:", "REPROVADO" if rel.falhou else "APROVADO (confira os itens REVISAR, se houver)")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Valida a saída do sanitizador contra o gabarito.")
    p.add_argument("--saida", required=True)
    p.add_argument("--gabarito", required=True)
    p.add_argument("--coluna-cep", default="CEP")
    p.add_argument("--coluna-cidade")
    p.add_argument("--coluna-uf")
    args = p.parse_args(argv)
    saida, _ = read_table(args.saida)
    gabarito, _ = read_table(args.gabarito)
    rel = validar(saida, gabarito, args.coluna_cep, args.coluna_cidade, args.coluna_uf)
    imprimir(rel)
    return 1 if rel.falhou else 0


if __name__ == "__main__":
    sys.exit(main())
