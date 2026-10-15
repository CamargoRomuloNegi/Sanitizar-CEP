import signal

import pandas as pd
import pytest

from cep_sanitizer import cli
from cep_sanitizer.client import BlockedError
from cep_sanitizer.io_utils import read_table

from conftest import FakeClient


@pytest.fixture
def entrada(tmp_path):
    p = tmp_path / "base.csv"
    p.write_text(
        "Nome;CEP;Situação\n"
        "Ana;01001000;Em uso\n"
        "Bia;1001000;Em uso\n"
        "Caio;20040020;Inativo\n"
        "Dani;ABC;Em uso\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture(autouse=True)
def _sigint_restaurado():
    anterior = signal.getsignal(signal.SIGINT)
    yield
    signal.signal(signal.SIGINT, anterior)


def _args(entrada, *extra):
    return [str(entrada), "--filtro-coluna", "Situação", "--filtro-valor", "Em uso", *extra]


def test_execucao_completa(entrada, monkeypatch):
    fake = FakeClient({"01001000": ("São Paulo", "SP")})
    monkeypatch.setattr(cli, "ViaCepClient", lambda cfg: fake)
    assert cli.main(_args(entrada, "--completar-zero")) == 0  # "1001000" perdeu o zero no Excel
    saida, _ = read_table(entrada.with_name("base_sanitizado.csv"))
    assert saida["Cidade Retornada"].tolist() == ["São Paulo", "São Paulo", "", "não encontrado"]
    assert "Status" not in saida.columns  # a base não tem colunas de Cidade/UF para comparar
    assert saida["CEP"].tolist() == ["01001000", "1001000", "20040020", "ABC"]
    assert fake.calls == ["01001000"]
    assert (entrada.parent / "base.csv.cache_cep.jsonl").exists()


def test_dry_run_nao_consulta_nem_grava(entrada, monkeypatch, capsys):
    monkeypatch.setattr(cli, "ViaCepClient", lambda cfg: pytest.fail("não deveria criar cliente"))
    assert cli.main(_args(entrada, "--dry-run")) == 0
    assert "CEPs únicos válidos: 1" in capsys.readouterr().out
    assert not entrada.with_name("base_sanitizado.csv").exists()


def test_bloqueio_grava_saida_parcial_e_retorna_2(entrada, monkeypatch):
    fake = FakeClient(fail_with=BlockedError("429"), fail_at=0)
    monkeypatch.setattr(cli, "ViaCepClient", lambda cfg: fake)
    assert cli.main(_args(entrada)) == 2
    saida, _ = read_table(entrada.with_name("base_sanitizado.csv"))
    assert saida["Diagnóstico CEP"].str.startswith("PENDENTE").sum() == 1  # "1001000" não é completado: inválido


def test_retomada_usa_o_cache(entrada, monkeypatch):
    primeiro = FakeClient({"01001000": ("São Paulo", "SP")})
    monkeypatch.setattr(cli, "ViaCepClient", lambda cfg: primeiro)
    cli.main(_args(entrada))
    segundo = FakeClient()
    monkeypatch.setattr(cli, "ViaCepClient", lambda cfg: segundo)
    assert cli.main(_args(entrada)) == 0
    assert segundo.calls == []


def test_intervalo_abaixo_do_piso_e_recusado(entrada, capsys):
    with pytest.raises(SystemExit):
        cli.main(_args(entrada, "--intervalo", "0.01"))


def test_coluna_cep_inexistente(entrada, capsys):
    assert cli.main([str(entrada), "--coluna-cep", "Nada"]) == 1


def test_filtro_incompleto(entrada):
    assert cli.main([str(entrada), "--filtro-coluna", "Situação"]) == 1


def test_arquivo_inexistente(tmp_path):
    assert cli.main([str(tmp_path / "nao_existe.csv")]) == 1
