"""Coluna Status: compara o retorno do ViaCEP com a Cidade e a UF da própria base."""

import signal

import pandas as pd
import pytest

from cep_sanitizer import cli
from cep_sanitizer.cache import JournalCache
from cep_sanitizer.io_utils import read_table
from cep_sanitizer.models import (
    ERRO_REDE,
    STATUS_NAO_ENCONTRADO,
    STATUS_OK,
    STATUS_REVER,
    LookupResult,
)
from cep_sanitizer.processor import (
    COL_CIDADE,
    COL_DIAG,
    COL_STATUS,
    COL_UF,
    _norm_place,
    detect_place_columns,
    sanitize,
)

from conftest import FakeClient

SP = ("São Paulo", "SP")


def _base(linhas):
    """linhas: (CEP, Cidade da base, UF da base, Usado)"""
    return pd.DataFrame(linhas, columns=["CEP", "Cidade", "UF", "Usado"])


def _rodar(df, client, **kw):
    kw.setdefault("filter_col", "Usado")
    kw.setdefault("numeric_gt", 0)
    return sanitize(df, "CEP", client, JournalCache(), city_col="Cidade", uf_col="UF", **kw)


def test_valores_do_status_sao_os_combinados():
    assert STATUS_NAO_ENCONTRADO == "Não Encontrado"
    assert STATUS_OK == "OK Cidade e UF"
    assert STATUS_REVER == "Rever - CIDADE ou UF"


@pytest.mark.parametrize(
    "cidade_base",
    [
        "Sao Paulo",                                  # sem acento (como a base real)
        "SAO PAULO",
        "sao paulo",
        "Sao Paulo                               ",   # preenchida até 40 caracteres
        "São Paulo",
        "  Sao   Paulo ",
    ],
)
def test_ok_ignora_acento_caixa_e_espacos(cidade_base):
    out, _ = _rodar(_base([("01001000", cidade_base, "SP", "3")]), FakeClient({"01001000": SP}))
    assert out.loc[0, COL_STATUS] == STATUS_OK
    assert out.loc[0, COL_DIAG] == "OK"


def test_ok_ignora_pontuacao_e_uf_em_minuscula():
    df = _base([("13500000", "Ji-Parana", "ro", "1"), ("13500001", "Santa Barbara d'Oeste", "SP", "1")])
    client = FakeClient({"13500000": ("Ji-Paraná", "RO"), "13500001": ("Santa Bárbara D'Oeste", "SP")})
    out, _ = _rodar(df, client)
    assert out[COL_STATUS].tolist() == [STATUS_OK, STATUS_OK]


def test_rever_quando_a_cidade_diverge_e_o_diagnostico_explica():
    out, stats = _rodar(_base([("01001000", "Campinas", "SP", "2")]), FakeClient({"01001000": SP}))
    assert out.loc[0, COL_STATUS] == STATUS_REVER
    assert out.loc[0, COL_DIAG].startswith("OK - difere da base:")
    assert "cidade base 'Campinas' x ViaCEP 'São Paulo'" in out.loc[0, COL_DIAG]
    assert "UF base" not in out.loc[0, COL_DIAG]
    assert stats.diag_counts == {"OK": 1}  # o código continua sendo OK (a nota vem depois)


def test_rever_quando_a_uf_diverge():
    out, _ = _rodar(_base([("01001000", "Sao Paulo", "RJ", "2")]), FakeClient({"01001000": SP}))
    assert out.loc[0, COL_STATUS] == STATUS_REVER
    assert "UF base 'RJ' x ViaCEP 'SP'" in out.loc[0, COL_DIAG]


def test_rever_quando_cidade_e_uf_divergem_aparecem_as_duas_notas():
    out, _ = _rodar(_base([("01001000", "Campinas", "RJ", "2")]), FakeClient({"01001000": SP}))
    assert "cidade base" in out.loc[0, COL_DIAG] and "UF base" in out.loc[0, COL_DIAG]


@pytest.mark.parametrize("cidade_base", ["", "NULL", "   "])
def test_rever_quando_a_base_nao_tem_cidade(cidade_base):
    out, _ = _rodar(_base([("01001000", cidade_base, "SP", "2")]), FakeClient({"01001000": SP}))
    assert out.loc[0, COL_STATUS] == STATUS_REVER


def test_nota_de_divergencia_convive_com_a_nota_do_zero_completado():
    df = _base([("1001000", "Campinas", "SP", "2")])
    out, _ = _rodar(df, FakeClient({"01001000": SP}), pad_leading_zero=True)
    diag = out.loc[0, COL_DIAG]
    assert diag.startswith("OK - CEP completado com zero à esquerda") and "| difere da base:" in diag
    assert out.loc[0, COL_STATUS] == STATUS_REVER


def test_nao_encontrado_para_branco_5_e_7_digitos_inexistente_e_erro():
    df = _base([
        ("        ", "Jordania", "MG", "5"),       # em branco
        ("11700   ", "Praia Grande", "SP", "1"),  # 5 dígitos
        ("4500000 ", "Vitoria da Conquista", "BA", "23"),  # 7 dígitos (truncado)
        ("99999999", "Qualquer", "SP", "1"),      # formato válido, não existe
        ("01001001", "Sao Paulo", "SP", "1"),     # erro de rede
    ])
    client = FakeClient({"01001001": LookupResult("01001001", ERRO_REDE, detalhe="Timeout")})
    out, stats = _rodar(df, client)
    assert (out[COL_STATUS] == STATUS_NAO_ENCONTRADO).all()
    assert (out[COL_CIDADE] == "não encontrado").all() and (out[COL_UF] == "não encontrado").all()
    assert out[COL_DIAG].str.split(" - ").str[0].tolist() == [
        "CEP_VAZIO", "CEP_INVALIDO", "CEP_INVALIDO", "NAO_EXISTE", "ERRO_REDE",
    ]
    assert stats.status_counts == {STATUS_NAO_ENCONTRADO: 5}


def test_fora_do_filtro_e_pendente_ficam_com_status_em_branco():
    df = _base([("01001000", "Sao Paulo", "SP", "0"), ("20040020", "Rio", "RJ", "4"), ("30130010", "BH", "MG", "4")])
    out, stats = _rodar(df, FakeClient({"20040020": ("Rio de Janeiro", "RJ")}), max_new_lookups=1)
    assert out[COL_STATUS].tolist() == ["", STATUS_REVER, ""]   # linha 0 fora do filtro, linha 2 pendente
    assert out[COL_DIAG].str.split(" - ").str[0].tolist() == ["FORA_DO_FILTRO", "OK", "PENDENTE"]
    assert out.loc[[0, 2], [COL_CIDADE, COL_UF]].eq("").all().all()
    assert stats.status_counts == {STATUS_REVER: 1}


def test_ordem_das_colunas_e_todas_as_linhas_na_saida():
    df = _base([("01001000", "Sao Paulo", "SP", "0"), ("01001001", "Sao Paulo", "SP", "3")])
    out, _ = _rodar(df, FakeClient({"01001001": SP}))
    assert list(out.columns) == ["CEP", "Cidade", "UF", "Usado", COL_CIDADE, COL_UF, COL_STATUS, COL_DIAG]
    assert len(out) == len(df)                                   # arquivo completo: nenhuma linha some
    assert out[["CEP", "Cidade", "UF", "Usado"]].equals(df)      # colunas originais intactas


def test_contagem_por_status():
    df = _base([("01001000", "Sao Paulo", "SP", "1"), ("01001001", "Campinas", "SP", "1"), ("", "x", "SP", "1")])
    _, stats = _rodar(df, FakeClient({"01001000": SP, "01001001": SP}))
    assert stats.status_counts == {STATUS_OK: 1, STATUS_REVER: 1, STATUS_NAO_ENCONTRADO: 1}


def test_sem_colunas_de_comparacao_nao_cria_status(fake_client_cls):
    df = _base([("01001000", "Sao Paulo", "SP", "1")])
    out, stats = sanitize(df, "CEP", fake_client_cls({"01001000": SP}), JournalCache())
    assert COL_STATUS not in out.columns and stats.status_counts == {}


def test_exige_as_duas_colunas_e_que_elas_existam(fake_client_cls):
    df = _base([("01001000", "Sao Paulo", "SP", "1")])
    with pytest.raises(ValueError):
        sanitize(df, "CEP", fake_client_cls(), JournalCache(), city_col="Cidade")
    with pytest.raises(KeyError):
        sanitize(df, "CEP", fake_client_cls(), JournalCache(), city_col="Cidade", uf_col="Nada")


def test_funcoes_auxiliares():
    assert _norm_place("Sao Paulo   ") == _norm_place("SÃO PAULO") == "sao paulo"
    assert _norm_place("Ji-Paraná") == "ji parana"
    assert detect_place_columns(["CEP", "Municipio", "Sigla UF"]) == ("Municipio", "Sigla UF")
    assert detect_place_columns(["CEP", "Cidade", "UF", "Bairro", "Usado"]) == ("Cidade", "UF")
    assert detect_place_columns(["CEP", "Nome"]) == (None, None)


# ---------------------------------------------------------------- CLI
@pytest.fixture(autouse=True)
def _sigint_restaurado():
    anterior = signal.getsignal(signal.SIGINT)
    yield
    signal.signal(signal.SIGINT, anterior)


@pytest.fixture
def np_csv(tmp_path):
    p = tmp_path / "NP_CEP.csv"
    p.write_text(
        "01001000;Sao Paulo;SP;Se;3\n"
        "01001001;Campinas;SP;Se;2\n"
        "01001002;Sao Paulo;SP;Se;0\n"
        "11700   ;Praia Grande;SP;Centro;1\n",
        encoding="utf-8-sig",
    )
    return p


def _args(np_csv, *extra):
    return [str(np_csv), "--sem-cabecalho", "--filtro-coluna", "Usado", "--filtro-maior-que", "0", *extra]


def test_cli_detecta_cidade_e_uf_e_gera_status(np_csv, monkeypatch, capsys):
    fake = FakeClient({"01001000": SP, "01001001": SP, "01001002": SP})
    monkeypatch.setattr(cli, "build_client", lambda fonte, intervalo: fake)
    assert cli.main(_args(np_csv)) == 0
    saida, _ = read_table(np_csv.with_name("NP_CEP_sanitizado.csv"))
    assert saida["Status"].tolist() == [STATUS_OK, STATUS_REVER, "", STATUS_NAO_ENCONTRADO]
    assert len(saida) == 4                                      # arquivo completo
    resumo = capsys.readouterr().out
    assert "Cidade da base='Cidade', UF da base='UF'" in resumo and f"{STATUS_REVER}=1" in resumo


def test_cli_sem_comparacao_nao_cria_status(np_csv, monkeypatch):
    monkeypatch.setattr(cli, "build_client", lambda fonte, intervalo: FakeClient({"01001000": SP, "01001001": SP}))
    assert cli.main(_args(np_csv, "--sem-comparacao")) == 0
    saida, _ = read_table(np_csv.with_name("NP_CEP_sanitizado.csv"))
    assert "Status" not in saida.columns


def test_cli_colunas_de_comparacao_informadas_a_mao(np_csv, monkeypatch):
    monkeypatch.setattr(cli, "build_client", lambda fonte, intervalo: FakeClient({"01001000": SP, "01001001": SP}))
    args = _args(np_csv, "--colunas", "CEP,Municipio,Estado,Bairro,Usado", "--coluna-cidade", "Municipio", "--coluna-uf", "Estado")
    assert cli.main(args) == 0
    saida, _ = read_table(np_csv.with_name("NP_CEP_sanitizado.csv"))
    assert saida["Status"].tolist()[:2] == [STATUS_OK, STATUS_REVER]


def test_cli_coluna_de_comparacao_inexistente(np_csv, capsys):
    assert cli.main(_args(np_csv, "--coluna-cidade", "Nada", "--dry-run")) == 1


def test_cli_dry_run_informa_a_comparacao(np_csv, capsys):
    assert cli.main(_args(np_csv, "--dry-run")) == 0
    assert "Comparação com a base: Cidade da base='Cidade'" in capsys.readouterr().out
