"""Filtro numérico/exclusão, arquivo sem cabeçalho e CEP de 7 dígitos truncado.

Os casos vêm da base NP_CEP.csv (exportação de banco, sem cabeçalho,
colunas CEP;Cidade;UF;Bairro;Usado, em que Usado = 0 não deve ser consultado).
"""

import io
import signal

import openpyxl
import pandas as pd
import pytest

from cep_sanitizer import cli
from cep_sanitizer.cache import JournalCache
from cep_sanitizer.io_utils import looks_headerless, read_table
from cep_sanitizer.models import CEP_INVALIDO, NAO_ENCONTRADO, OK
from cep_sanitizer.normalizer import normalize_cep
from cep_sanitizer.processor import COL_CIDADE, COL_DIAG, build_plan, sanitize

from conftest import FakeClient

SP = ("São Paulo", "SP")
NOMES = ["CEP", "Cidade", "UF", "Bairro", "Usado"]

# Trecho no formato real da base (sem cabeçalho; 1ª linha com CEP em branco e campos largos).
TRECHO = (
    '"        ";Jordania;MG;NULL;5\n'
    '"  769138";Ji-Parana;RO;RIACHUELO;0\n'
    '" 6071475";Fortaleza;CE;DENDE;0\n'
    "00029100;Vila Velha;ES;Camboapina;1\n"
    '01001000;"Sao Paulo                               ";SP;"Se                            ";34\n'
    '01001001;"Sao Paulo                               ";SP;"Se                            ";4\n'
    '01002001;"Sao Paulo                               ";SP;"Se                            ";0\n'
    "4500000 ;Vitoria da Conquista;BA;Centro;23\n"
    "11700   ;Praia Grande;SP;Centro;1\n"
)


def _df(usado, ceps=None):
    ceps = ceps or [f"0100100{i}" for i in range(len(usado))]
    return pd.DataFrame({"CEP": ceps, "Usado": usado})


# ---------------------------------------------------------------- filtro numérico
def test_numerico_maior_que_zero_deixa_fora_zeros_e_nao_consulta(fake_client_cls):
    df = _df(["0", "1", "5", "00", "0.0", "34"])
    client = fake_client_cls(default=lambda c: __import__("cep_sanitizer.models", fromlist=["x"]).LookupResult(c, OK, "X", "SP"))
    out, stats = sanitize(df, "CEP", client, JournalCache(), filter_col="Usado", numeric_gt=0)
    assert client.calls == ["01001001", "01001002", "01001005"]
    assert out[COL_DIAG].str.split(" - ").str[0].tolist() == [
        "FORA_DO_FILTRO", "OK", "OK", "FORA_DO_FILTRO", "FORA_DO_FILTRO", "OK",
    ]
    assert out.loc[out[COL_DIAG].str.startswith("FORA"), COL_CIDADE].eq("").all()
    assert stats.rows_in_scope == 3


def test_numerico_aceita_virgula_decimal_e_espacos():
    plan = build_plan(_df([" 2 ", "0,5", "0,0", "-1"]), "CEP", "Usado", numeric_gt=0)
    assert plan.mask.tolist() == [True, True, False, False]


def test_numerico_vazio_ou_texto_fica_fora_e_e_contado():
    plan = build_plan(_df(["3", "", "NULL", "abc", "0"]), "CEP", "Usado", numeric_gt=0)
    assert plan.mask.tolist() == [True, False, False, False, False]
    assert plan.filter_non_numeric == 3


def test_excluir_valores_mantem_vazios_no_escopo():
    plan = build_plan(_df(["0", "1", "", "2"]), "CEP", "Usado", exclude_values=["0"])
    assert plan.mask.tolist() == [False, True, True, True]


def test_exatamente_uma_regra_de_filtro():
    df = _df(["0", "1"])
    with pytest.raises(ValueError):
        build_plan(df, "CEP", "Usado")                                   # coluna sem regra
    with pytest.raises(ValueError):
        build_plan(df, "CEP", "Usado", ["1"], exclude_values=["0"])      # duas regras
    with pytest.raises(ValueError):
        build_plan(df, "CEP", None, numeric_gt=0)                        # regra sem coluna
    with pytest.raises(ValueError):
        build_plan(df, "CEP", "Usado", exclude_values=[])                # lista vazia


# ---------------------------------------------------------------- CEP de 7 dígitos
def test_cep_de_7_digitos_e_invalido_por_padrao_e_completa_so_com_opt_in():
    n = normalize_cep("4500000 ")                            # Vitória da Conquista/BA, na base
    assert n.status == CEP_INVALIDO and n.cep is None and "truncado" in n.detalhe
    assert normalize_cep("4500000 ", pad_leading_zero=True).cep == "04500000"


def test_plano_respeita_pad_leading_zero(fake_client_cls):
    df = _df(["1", "1"], ["4500000 ", "01001000"])
    com = build_plan(df, "CEP", pad_leading_zero=True)
    sem = build_plan(df, "CEP", pad_leading_zero=False)
    assert com.ceps == ["04500000", "01001000"] and sem.ceps == ["01001000"]


# ---------------------------------------------------------------- arquivo sem cabeçalho
def test_sem_cabecalho_preserva_primeiro_registro(tmp_path):
    p = tmp_path / "np.csv"
    p.write_text(TRECHO, encoding="utf-8-sig")
    df, meta = read_table(p, header=False, names=NOMES)
    assert df.columns.tolist() == NOMES and len(df) == 9
    assert df.loc[0, "Cidade"] == "Jordania" and df.loc[0, "CEP"] == "        "
    assert df.loc[4, "Cidade"].strip() == "Sao Paulo" and df.loc[4, "Usado"] == "34"
    assert meta.sep == ";" and meta.encoding == "utf-8-sig"


def test_com_cabecalho_engole_o_primeiro_registro_e_a_heuristica_avisa(tmp_path):
    p = tmp_path / "np.csv"
    p.write_text(TRECHO, encoding="utf-8-sig")
    df, _ = read_table(p)  # erro de uso proposital: header=True
    assert len(df) == 8 and looks_headerless(df.columns)


def test_nomes_com_quantidade_errada_levanta_erro(tmp_path):
    p = tmp_path / "np.csv"
    p.write_text(TRECHO, encoding="utf-8-sig")
    with pytest.raises(ValueError, match="5 colunas"):
        read_table(p, header=False, names=["CEP", "Cidade"])


def test_xlsx_sem_cabecalho():
    wb = openpyxl.Workbook()
    wb.active.append(["01001000", "Sao Paulo", "SP", "Se", "3"])
    wb.active.append(["01001001", "Sao Paulo", "SP", "Se", "0"])
    buf = io.BytesIO()
    wb.save(buf)
    df, meta = read_table(buf.getvalue(), filename="a.xlsx", header=False, names=NOMES)
    assert len(df) == 2 and df.loc[0, "CEP"] == "01001000" and meta.kind == "xlsx"


# ---------------------------------------------------------------- CLI ponta a ponta no formato real
@pytest.fixture(autouse=True)
def _sigint_restaurado():
    anterior = signal.getsignal(signal.SIGINT)
    yield
    signal.signal(signal.SIGINT, anterior)


@pytest.fixture
def np_csv(tmp_path):
    p = tmp_path / "NP_CEP.csv"
    p.write_text(TRECHO, encoding="utf-8-sig")
    return p


def _cli(np_csv, *extra):
    return [str(np_csv), "--sem-cabecalho", "--filtro-coluna", "Usado", "--filtro-maior-que", "0", *extra]


def test_cli_base_np_cep_filtra_usado_zero_e_nao_adivinha_cep_truncado(np_csv, monkeypatch):
    fake = FakeClient({"01001000": SP, "01001001": SP})
    monkeypatch.setattr(cli, "ViaCepClient", lambda cfg: fake)
    assert cli.main(_cli(np_csv)) == 0  # padrão: CEP de 7 dígitos NÃO é completado

    saida, _ = read_table(np_csv.with_name("NP_CEP_sanitizado.csv"))
    assert list(saida.columns)[:5] == NOMES and len(saida) == 9          # nenhuma linha perdida
    assert saida["Cidade"].tolist()[0] == "Jordania"                     # 1º registro preservado
    codigos = saida["Diagnóstico CEP"].str.split(" - ").str[0].tolist()
    assert codigos == [
        "CEP_VAZIO",        # Jordania, usado 5, CEP em branco
        "FORA_DO_FILTRO",   # usado 0
        "FORA_DO_FILTRO",   # usado 0
        "NAO_EXISTE",       # 00029100 (formato válido; o fake não o conhece)
        "OK", "OK",         # 01001000, 01001001
        "FORA_DO_FILTRO",   # 01002001, usado 0
        "CEP_INVALIDO",     # "4500000 ": 7 dígitos, não completado
        "CEP_INVALIDO",     # "11700   ": 5 dígitos
    ]
    assert fake.calls == ["00029100", "01001000", "01001001"]            # só o que é usado e válido
    assert (saida.loc[saida["Usado"] == "0", "Cidade Retornada"] == "").all()
    assert saida.loc[7, "Cidade Retornada"] == NAO_ENCONTRADO
    # colunas originais intactas, inclusive o preenchimento de espaços da base
    assert saida.loc[4, "Cidade"] == "Sao Paulo                               "


def test_cli_dry_run_mostra_numeros_sem_consultar(np_csv, monkeypatch, capsys):
    monkeypatch.setattr(cli, "ViaCepClient", lambda cfg: pytest.fail("não deveria criar cliente"))
    assert cli.main(_cli(np_csv, "--dry-run")) == 0
    out = capsys.readouterr().out
    assert "Linhas no escopo: 6" in out and "fora do filtro (não consultadas): 3" in out
    assert "CEPs únicos válidos: 3" in out and "Usado > 0" in out


def test_cli_filtros_conflitantes_sao_recusados(np_csv):
    assert cli.main(_cli(np_csv, "--filtro-excluir", "0")) == 1
    assert cli.main([str(np_csv), "--sem-cabecalho", "--filtro-maior-que", "0"]) == 1


def test_cli_avisa_quando_parece_faltar_o_sem_cabecalho(np_csv, capsys):
    cli.main([str(np_csv), "--coluna-cep", "5", "--dry-run"])
    assert "--sem-cabecalho" in capsys.readouterr().err
