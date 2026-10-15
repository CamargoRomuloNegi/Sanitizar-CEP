"""Gera a massa de teste de 1.000 linhas e o gabarito correspondente.

A massa imita o layout da base real: SEM cabeçalho, separador ';', colunas
``CEP;Cidade;UF;Bairro;Usado``, cidade sem acento (às vezes preenchida com espaços até 40
caracteres) e CEPs de largura fixa (ex.: ``"11700   "``). Rode com:

    python -m cep_sanitizer dados_exemplo/massa_teste_1000.csv --sem-cabecalho \\
        --filtro-coluna Usado --filtro-maior-que 0 -o saida_teste.csv
    python tools/validar_resultado.py --saida saida_teste.csv \\
        --gabarito dados_exemplo/massa_teste_1000_gabarito.csv

Uso:
    python tools/gerar_massa_teste.py                      # massa sintética (padrão)
    python tools/gerar_massa_teste.py --base NP_CEP.csv --sem-cabecalho \\
        --filtro-coluna Usado --filtro-maior-que 0         # amostra de 1.000 linhas da base real

Massa sintética (seed fixa => sempre o mesmo arquivo), 1.000 linhas:

    CEPs reais de capitais (170): limpo, com hífen, com ponto e hífen, com espaços,
        "20040020.0" do Excel; e variantes em que a CIDADE (20) ou a UF (20) da base
        divergem do ViaCEP => Status "Rever - CIDADE ou UF"
    CEP em branco (30), de 5 dígitos (30), de 7 dígitos (30) e outros inválidos (90)
        => "não encontrado" / Status "Não Encontrado"
    CEP de formato válido que não deve existir (60), como 99999999
    CEPs aleatórios plausíveis por faixa de UF (290): sem gabarito de cidade, só plausibilidade
    Usado = 0 (300): FORA do escopo, não consultadas, Status em branco

Coluna ``confianca`` do gabarito:
    deterministico  decorre das regras do app (inválido, vazio, fora do filtro): divergência = FAIL
    verificado      confirmado pela documentação do ViaCEP: divergência = FAIL
    provavel        cidade/UF esperadas de memória; confirmar na 1ª execução: divergência = REVISAR
    sem_gabarito    qualquer resposta válida serve; só a plausibilidade da UF é checada
"""

from __future__ import annotations

import argparse
import random
import sys
import unicodedata
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cep_sanitizer.cli import COLUNAS_PADRAO, filter_rule  # noqa: E402
from cep_sanitizer.io_utils import read_table  # noqa: E402
from cep_sanitizer.models import STATUS_NAO_ENCONTRADO, STATUS_OK, STATUS_REVER  # noqa: E402
from cep_sanitizer.processor import build_plan, detect_place_columns  # noqa: E402
from tools.faixas_uf import FAIXAS_UF  # noqa: E402

NOMES = COLUNAS_PADRAO.split(",")  # CEP, Cidade, UF, Bairro, Usado

# (CEP, cidade, UF, confianca)
# 01001-000 (Praça da Sé) é o exemplo da documentação do ViaCEP. Os demais são CEPs
# centrais de capitais, informados de memória: conferir na primeira execução real.
ANCORAS: list[tuple[str, str, str, str]] = [
    ("01001000", "São Paulo", "SP", "verificado"),
    ("01310100", "São Paulo", "SP", "provavel"),
    ("20040020", "Rio de Janeiro", "RJ", "provavel"),
    ("30130010", "Belo Horizonte", "MG", "provavel"),
    ("70040010", "Brasília", "DF", "provavel"),
    ("80010000", "Curitiba", "PR", "provavel"),
    ("90010000", "Porto Alegre", "RS", "provavel"),
    ("40020000", "Salvador", "BA", "provavel"),
    ("66010000", "Belém", "PA", "provavel"),
    ("88010001", "Florianópolis", "SC", "provavel"),
]
ANCORAS_SEM_ZERO_INICIAL = [a for a in ANCORAS if not a[0].startswith("0")]
UFS = sorted({a[2] for a in ANCORAS})
CIDADES_ERRADAS = ["Campinas", "Santos", "Niteroi", "Uberlandia", "Londrina", "Joinville"]

NAO_EXISTE_DOCUMENTADO = "99999999"  # exemplo da documentação do ViaCEP
NAO_EXISTE_PROVAVEIS = [
    "99999990", "99999991", "11111111", "22222222", "33333333",
    "44444444", "55555555", "66666666", "77777777", "88888888",
]
INVALIDOS_FIXOS = [
    "ABCDEFGH", "95010A10", "95010 10", "950100100", "123456", "12345-67", "00000000",
    "0000000", "N/A", "-", "sem cep", "1234567890123", "1.2.3", "12-345-678", "CEP",
]
VAZIOS = ["", " ", "        ", "nan", "NULL"]

COLUNAS_GABARITO = [
    "linha", "cenario", "confianca", "cep_original",
    "esperado_diag", "esperado_status", "esperado_cidade", "esperado_uf",
]
NAO_ENCONTRADO = "não encontrado"


def _sem_acento(texto: str) -> str:
    nfkd = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _cep_aleatorio(rng: random.Random) -> tuple[str, str]:
    """(CEP de 8 dígitos, UF da faixa sorteada)."""
    pesos = [int(fim) - int(ini) for _, ini, fim in FAIXAS_UF]
    uf, ini, fim = rng.choices(FAIXAS_UF, weights=pesos)[0]
    return f"{rng.randint(int(ini), int(fim)):08d}", uf


def _invalido(rng: random.Random) -> str:
    tipo = rng.choice(["fixo", "fixo", "9dig", "6dig", "letra"])
    if tipo == "9dig":
        return str(rng.randint(100000000, 999999999))
    if tipo == "6dig":
        return str(rng.randint(100000, 999999))
    if tipo == "letra":
        digitos = list(f"{rng.randint(10000000, 99999999)}")
        digitos[rng.randrange(8)] = rng.choice("ABCDEFGHJKLMNPRSTUVXZ")
        return "".join(digitos)
    return rng.choice(INVALIDOS_FIXOS)


def gerar(seed: int = 2026) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Devolve (massa, gabarito); mesma seed => mesmo resultado."""
    rng = random.Random(seed)
    linhas: list[dict] = []

    def cidade_base(cidade: str) -> str:
        """Como a base real grava: sem acento; às vezes MAIÚSCULA ou preenchida até 40 caracteres."""
        nome = _sem_acento(cidade)
        return rng.choice([nome, nome.upper(), nome.ljust(40)])

    def add(cenario, conf, cep, cidade, uf, usado, diag, status, e_cidade, e_uf):
        linhas.append(dict(
            cenario=cenario, confianca=conf, CEP=cep, Cidade=cidade, UF=uf,
            Bairro=rng.choice(["Centro", "Se", "Jardim America", "NULL"]), Usado=usado,
            esperado_diag=diag, esperado_status=status, esperado_cidade=e_cidade, esperado_uf=e_uf,
        ))

    def em_uso() -> str:
        return str(rng.randint(1, 40))

    def ancora(pool=ANCORAS):
        return rng.choice(pool)

    def add_ancora(cenario, cep_fmt, c, cidade=None, uf=None, status=STATUS_OK):
        add(cenario, c[3], cep_fmt(c[0]), cidade if cidade is not None else cidade_base(c[1]),
            uf if uf is not None else c[2], em_uso(), "OK", status, c[1], c[2])

    for _ in range(50):
        add_ancora("ANCORA_LIMPO", lambda x: x, ancora())
    for _ in range(30):
        add_ancora("ANCORA_HIFEN", lambda x: f"{x[:5]}-{x[5:]}", ancora())
    for _ in range(20):
        add_ancora("ANCORA_PONTO_HIFEN", lambda x: f"{x[:2]}.{x[2:5]}-{x[5:]}", ancora())
    for _ in range(20):
        add_ancora("ANCORA_ESPACOS", lambda x: rng.choice([f"  {x} ", f"{x}   ", f" {x}"]), ancora())
    for _ in range(10):
        add_ancora("ANCORA_FLOAT_EXCEL", lambda x: f"{int(x)}.0", ancora(ANCORAS_SEM_ZERO_INICIAL))
    for _ in range(20):  # a cidade da base diverge do ViaCEP
        add_ancora("ANCORA_CIDADE_DIVERGENTE", lambda x: x, ancora(),
                   cidade=rng.choice(CIDADES_ERRADAS), status=STATUS_REVER)
    for _ in range(20):  # a UF da base diverge do ViaCEP
        c = ancora()
        add_ancora("ANCORA_UF_DIVERGENTE", lambda x: x, c,
                   uf=rng.choice([u for u in UFS if u != c[2]]), status=STATUS_REVER)

    nao_encontrado = ("CEP_VAZIO", STATUS_NAO_ENCONTRADO, NAO_ENCONTRADO, NAO_ENCONTRADO)
    for _ in range(30):
        add("CEP_VAZIO", "deterministico", rng.choice(VAZIOS), "Cidade Teste", rng.choice(UFS),
            em_uso(), *nao_encontrado)
    for _ in range(30):  # "11700   ": 5 dígitos em campo de 8
        cep5 = _cep_aleatorio(rng)[0][:5]
        add("CEP_5_DIGITOS", "deterministico", cep5.ljust(8), "Cidade Teste", rng.choice(UFS),
            em_uso(), "CEP_INVALIDO", STATUS_NAO_ENCONTRADO, NAO_ENCONTRADO, NAO_ENCONTRADO)
    for _ in range(30):  # "4500000 ": CEP truncado (perdeu o último dígito)
        origem = ancora()[0] if rng.random() < 0.5 else _cep_aleatorio(rng)[0]
        add("CEP_7_DIGITOS", "deterministico", origem[:7].ljust(8), "Cidade Teste", rng.choice(UFS),
            em_uso(), "CEP_INVALIDO", STATUS_NAO_ENCONTRADO, NAO_ENCONTRADO, NAO_ENCONTRADO)
    for _ in range(90):
        add("CEP_INVALIDO", "deterministico", _invalido(rng), "Cidade Teste", rng.choice(UFS),
            em_uso(), "CEP_INVALIDO", STATUS_NAO_ENCONTRADO, NAO_ENCONTRADO, NAO_ENCONTRADO)

    for _ in range(20):
        add("NAO_EXISTE_DOC", "verificado", NAO_EXISTE_DOCUMENTADO, "Cidade Teste", rng.choice(UFS),
            em_uso(), "NAO_EXISTE", STATUS_NAO_ENCONTRADO, NAO_ENCONTRADO, NAO_ENCONTRADO)
    for _ in range(40):
        add("NAO_EXISTE_PROVAVEL", "provavel", rng.choice(NAO_EXISTE_PROVAVEIS), "Cidade Teste",
            rng.choice(UFS), em_uso(), "NAO_EXISTE", STATUS_NAO_ENCONTRADO, NAO_ENCONTRADO, NAO_ENCONTRADO)

    for _ in range(290):
        cep, uf = _cep_aleatorio(rng)
        add("EXPLORATORIO", "sem_gabarito", cep, "Cidade Teste", uf, em_uso(), "*", "*", "*", "*")

    for _ in range(300):  # Usado = 0: fora do escopo, mesmo que o CEP seja válido e consultado em outra linha
        cep = ancora()[0] if rng.random() < 0.5 else _cep_aleatorio(rng)[0]
        add("FORA_DO_FILTRO", "deterministico", cep, "Cidade Teste", rng.choice(UFS),
            rng.choice(["0"] * 9 + ["00"]), "FORA_DO_FILTRO", "", "", "")

    rng.shuffle(linhas)
    for i, linha in enumerate(linhas, start=1):
        linha["linha"] = i
        linha["cep_original"] = linha["CEP"]

    df = pd.DataFrame(linhas)
    return df[NOMES].astype(str), df[COLUNAS_GABARITO].astype(str)


def amostrar_base(caminho, args):
    """Amostra ``args.n`` linhas da base real; o gabarito só cobre o que é determinístico."""
    names = [c.strip() for c in args.colunas.split(",")] if args.sem_cabecalho else None
    df, _ = read_table(caminho, header=not args.sem_cabecalho, names=names)
    amostra = df.sample(n=min(args.n, len(df)), random_state=args.seed).reset_index(drop=True)
    rule, _ = filter_rule(args)
    plan = build_plan(
        amostra, args.coluna_cep, args.filtro_coluna, rule.get("filter_values"),
        exclude_values=rule.get("exclude_values"), numeric_gt=rule.get("numeric_gt"),
        pad_leading_zero=False,
    )
    com_status = all(detect_place_columns(amostra.columns))
    registros = []
    for i in range(len(amostra)):
        cep_original = amostra.at[i, args.coluna_cep]
        diag, status, cidade, uf, conf = "*", "*", "*", "*", "sem_gabarito"
        if not bool(plan.mask.iloc[i]):
            diag, status, cidade, uf, conf = "FORA_DO_FILTRO", "", "", "", "deterministico"
        else:
            norm = plan.norm_map[str(cep_original)]
            if norm.status != "OK":
                diag, cidade, uf, conf = norm.status, NAO_ENCONTRADO, NAO_ENCONTRADO, "deterministico"
                status = STATUS_NAO_ENCONTRADO if com_status else "*"
        registros.append(dict(
            linha=i + 1, cenario="BASE_REAL", confianca=conf, cep_original=cep_original,
            esperado_diag=diag, esperado_status=status, esperado_cidade=cidade, esperado_uf=uf,
        ))
    return amostra, pd.DataFrame(registros, columns=COLUNAS_GABARITO).astype(str)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Gera massa de teste de 1.000 CEPs e o gabarito.")
    p.add_argument("--saida-dir", default=str(ROOT / "dados_exemplo"))
    p.add_argument("--seed", type=int, default=2026)
    p.add_argument("--n", type=int, default=1000, help="tamanho da amostra (modo --base)")
    p.add_argument("--base", help="CSV/XLSX real para amostrar (em vez da massa sintética)")
    p.add_argument("--coluna-cep", default="CEP")
    p.add_argument("--sem-cabecalho", action="store_true")
    p.add_argument("--colunas", default=COLUNAS_PADRAO)
    p.add_argument("--filtro-coluna")
    p.add_argument("--filtro-valor", action="append")
    p.add_argument("--filtro-excluir", action="append")
    p.add_argument("--filtro-maior-que", type=float)
    args = p.parse_args(argv)

    destino = Path(args.saida_dir)
    destino.mkdir(parents=True, exist_ok=True)

    if args.base:
        amostra, gabarito = amostrar_base(args.base, args)
        arq_massa = destino / f"amostra_base_{len(amostra)}.csv"
        arq_gab = destino / f"amostra_base_{len(amostra)}_gabarito.csv"
        amostra.to_csv(arq_massa, index=False, header=not args.sem_cabecalho, sep=";", encoding="utf-8-sig")
    else:
        massa, gabarito = gerar(args.seed)
        arq_massa = destino / "massa_teste_1000.csv"
        arq_gab = destino / "massa_teste_1000_gabarito.csv"
        massa.to_csv(arq_massa, index=False, header=False, sep=";", encoding="utf-8-sig")

    gabarito.to_csv(arq_gab, index=False, sep=";", encoding="utf-8-sig")
    print(f"Massa:    {arq_massa}")
    print(f"Gabarito: {arq_gab}")
    print(gabarito["cenario"].value_counts().to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
