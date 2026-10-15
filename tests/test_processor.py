import time

import pandas as pd
import pytest

from cep_sanitizer.cache import JournalCache
from cep_sanitizer.client import BlockedError, ServiceUnavailableError
from cep_sanitizer.models import ERRO_REDE, NAO_ENCONTRADO, NAO_EXISTE, OK, LookupResult
from cep_sanitizer.processor import (
    COL_CIDADE,
    COL_DIAG,
    COL_UF,
    build_plan,
    estimate_work,
    sanitize,
)

SP = ("São Paulo", "SP")
RJ = ("Rio de Janeiro", "RJ")


def _df(ceps, situacoes=None):
    situacoes = situacoes or ["Em uso"] * len(ceps)
    return pd.DataFrame({"Nome": [f"r{i}" for i in range(len(ceps))], "CEP": ceps, "Situação": situacoes})


def test_uma_consulta_por_cep_unico_mesmo_com_formatos_diferentes(fake_client_cls):
    df = _df(["01001000", "01001-000", "1001000", "01.001-000", "1001000.0", "01001000"])
    client = fake_client_cls({"01001000": SP})
    out, stats = sanitize(df, "CEP", client, JournalCache(), pad_leading_zero=True)  # planilha vinda do Excel
    assert client.calls == ["01001000"]
    assert (out[COL_CIDADE] == "São Paulo").all() and (out[COL_UF] == "SP").all()
    assert stats.unique_ceps == 1 and stats.queried == 1 and stats.complete


def test_colunas_novas_e_original_intacto(fake_client_cls):
    df = _df(["01001000", "20040020"])
    antes = df.copy()
    client = fake_client_cls({"01001000": SP, "20040020": RJ})
    out, _ = sanitize(df, "CEP", client, JournalCache())
    pd.testing.assert_frame_equal(df, antes)  # entrada não é alterada
    assert list(out.columns) == ["Nome", "CEP", "Situação", COL_CIDADE, COL_UF, COL_DIAG]
    assert out["CEP"].tolist() == ["01001000", "20040020"]  # zeros à esquerda preservados
    assert out[COL_CIDADE].tolist() == ["São Paulo", "Rio de Janeiro"]
    assert out[COL_UF].tolist() == ["SP", "RJ"]
    assert out[COL_DIAG].tolist() == ["OK", "OK"]


def test_invalidos_e_vazios_viram_nao_encontrado_sem_chamar_api(fake_client_cls):
    df = _df(["", "ABC", "123456", None, "00000000"])
    client = fake_client_cls()
    out, stats = sanitize(df, "CEP", client, JournalCache())
    assert client.calls == []
    assert (out[COL_CIDADE] == NAO_ENCONTRADO).all() and (out[COL_UF] == NAO_ENCONTRADO).all()
    assert out[COL_DIAG].str.split(" - ").str[0].tolist() == [
        "CEP_VAZIO", "CEP_INVALIDO", "CEP_INVALIDO", "CEP_VAZIO", "CEP_INVALIDO",
    ]
    assert stats.complete


def test_cep_que_nao_existe(fake_client_cls):
    out, _ = sanitize(_df(["99999999"]), "CEP", fake_client_cls(), JournalCache())
    assert out.loc[0, COL_CIDADE] == NAO_ENCONTRADO
    assert out.loc[0, COL_UF] == NAO_ENCONTRADO
    assert out.loc[0, COL_DIAG].startswith(NAO_EXISTE)


def test_zero_completado_aparece_no_diagnostico(fake_client_cls):
    client = fake_client_cls({"01001000": SP})
    out, _ = sanitize(_df(["1001000"]), "CEP", client, JournalCache(), pad_leading_zero=True)
    assert out.loc[0, COL_DIAG].startswith("OK - CEP completado com zero à esquerda")
    assert "01001000" in out.loc[0, COL_DIAG]
    assert out.loc[0, "CEP"] == "1001000"  # o original não é reescrito


def test_por_padrao_cep_de_7_digitos_nao_e_completado_nem_consultado(fake_client_cls):
    client = fake_client_cls({"01001000": SP})
    out, _ = sanitize(_df(["1001000"]), "CEP", client, JournalCache())
    assert client.calls == []
    assert out.loc[0, COL_CIDADE] == NAO_ENCONTRADO
    assert out.loc[0, COL_DIAG].startswith("CEP_INVALIDO - 7 dígitos")


def test_filtro_em_uso_nao_consulta_nem_preenche_o_resto(fake_client_cls):
    df = _df(
        ["01001000", "20040020", "01001000", "30130010"],
        ["Em uso", "Inativo", "Inativo", "EM  USO "],  # variações de caixa/espaço
    )
    client = fake_client_cls({"01001000": SP, "20040020": RJ, "30130010": ("Belo Horizonte", "MG")})
    out, stats = sanitize(df, "CEP", client, JournalCache(), filter_col="Situação", filter_values=["em uso"])
    assert sorted(client.calls) == ["01001000", "30130010"]  # 20040020 só aparece em linha inativa
    assert out[COL_CIDADE].tolist() == ["São Paulo", "", "", "Belo Horizonte"]
    assert out[COL_UF].tolist() == ["SP", "", "", "MG"]
    assert out[COL_DIAG].str.split(" - ").str[0].tolist() == ["OK", "FORA_DO_FILTRO", "FORA_DO_FILTRO", "OK"]
    assert stats.rows_in_scope == 2 and stats.complete


def test_cache_evita_chamadas(fake_client_cls):
    cache = JournalCache()
    cache.put(LookupResult("01001000", OK, "São Paulo", "SP"))
    client = fake_client_cls()
    out, stats = sanitize(_df(["01001000"]), "CEP", client, cache)
    assert client.calls == []
    assert stats.from_cache == 1 and stats.queried == 0
    assert out.loc[0, COL_CIDADE] == "São Paulo"


def test_erro_transitorio_vira_nao_encontrado_mas_nao_vai_para_o_cache(fake_client_cls):
    cache = JournalCache()
    falha = fake_client_cls({"01001000": LookupResult("01001000", ERRO_REDE, detalhe="Timeout")})
    out, _ = sanitize(_df(["01001000"]), "CEP", falha, cache)
    assert out.loc[0, COL_CIDADE] == NAO_ENCONTRADO
    assert out.loc[0, COL_DIAG].startswith(ERRO_REDE)
    assert len(cache) == 0

    ok = fake_client_cls({"01001000": SP})
    out2, _ = sanitize(_df(["01001000"]), "CEP", ok, cache)  # próxima execução tenta de novo
    assert ok.calls == ["01001000"] and out2.loc[0, COL_CIDADE] == "São Paulo"


def test_limite_por_execucao_deixa_pendentes_e_retoma(fake_client_cls):
    ceps = [f"0100100{i}" for i in range(5)]
    cache = JournalCache()
    primeiro = fake_client_cls(default=lambda c: LookupResult(c, OK, "X", "SP"))
    out, stats = sanitize(_df(ceps), "CEP", primeiro, cache, max_new_lookups=2)
    assert stats.queried == 2 and stats.pending_ceps == 3 and not stats.complete
    assert out[COL_DIAG].str.startswith("PENDENTE").sum() == 3
    assert out.loc[out[COL_DIAG].str.startswith("PENDENTE"), COL_CIDADE].eq("").all()

    segundo = fake_client_cls(default=lambda c: LookupResult(c, OK, "X", "SP"))
    out2, stats2 = sanitize(_df(ceps), "CEP", segundo, cache, max_new_lookups=10)
    assert len(segundo.calls) == 3  # só o que faltava
    assert stats2.complete and (out2[COL_DIAG] == "OK").all()


def test_bloqueio_para_salva_progresso_e_marca_pendentes(fake_client_cls):
    ceps = [f"0100100{i}" for i in range(6)]
    cache = JournalCache()
    client = fake_client_cls(
        default=lambda c: LookupResult(c, OK, "X", "SP"),
        fail_with=BlockedError("429 seguidos"), fail_at=3,
    )
    out, stats = sanitize(_df(ceps), "CEP", client, cache)
    assert "bloqueio" in stats.aborted_reason and not stats.complete
    assert len(cache) == 3 and stats.queried == 3 and stats.pending_ceps == 3
    assert out[COL_DIAG].str.startswith("PENDENTE").sum() == 3


def test_servico_indisponivel_para(fake_client_cls):
    client = fake_client_cls(fail_with=ServiceUnavailableError("fora do ar"), fail_at=0)
    _, stats = sanitize(_df(["01001000"]), "CEP", client, JournalCache())
    assert "indisponível" in stats.aborted_reason and stats.pending_ceps == 1


def test_interrupcao_pelo_usuario(fake_client_cls):
    ceps = [f"0100100{i}" for i in range(4)]
    client = fake_client_cls(default=lambda c: LookupResult(c, OK, "X", "SP"))
    stop_after = {"n": 0}

    def should_stop():
        stop_after["n"] += 1
        return stop_after["n"] > 2

    _, stats = sanitize(_df(ceps), "CEP", client, JournalCache(), should_stop=should_stop)
    assert stats.aborted_reason == "interrompido pelo usuário"
    assert stats.queried == 2 and stats.pending_ceps == 2


def test_callback_de_progresso(fake_client_cls):
    ceps = [f"0100100{i}" for i in range(3)]
    eventos = []
    client = fake_client_cls(default=lambda c: LookupResult(c, OK, "X", "SP"))
    sanitize(_df(ceps), "CEP", client, JournalCache(), progress=lambda d, t, r: eventos.append((d, t, r.cep)))
    assert eventos == [(1, 3, ceps[0]), (2, 3, ceps[1]), (3, 3, ceps[2])]


def test_erros_de_configuracao():
    df = _df(["01001000"])
    with pytest.raises(KeyError):
        build_plan(df, "NaoExiste")
    with pytest.raises(KeyError):
        build_plan(df, "CEP", "NaoExiste", ["Em uso"])
    with pytest.raises(ValueError):
        build_plan(df, "CEP", "Situação", [])


def test_estimate_work(fake_client_cls):
    df = _df(["01001000", "01001000", "20040020", "ABC"], ["Em uso", "Em uso", "Em uso", "Inativo"])
    cache = JournalCache()
    cache.put(LookupResult("01001000", OK, "São Paulo", "SP"))
    plan = build_plan(df, "CEP", "Situação", ["Em uso"])
    assert estimate_work(plan, cache) == {
        "rows_total": 4, "rows_in_scope": 3, "rows_out_of_scope": 1,
        "unique_ceps": 2, "cached": 1, "to_query": 1,
    }


def test_indice_nao_sequencial_e_valores_nulos(fake_client_cls):
    df = pd.DataFrame({"CEP": ["01001000", None, "20040020"], "Situação": ["Em uso"] * 3}, index=[10, 5, 7])
    client = fake_client_cls({"01001000": SP, "20040020": RJ})
    out, _ = sanitize(df, "CEP", client, JournalCache(), filter_col="Situação", filter_values=["Em uso"])
    assert out[COL_CIDADE].tolist() == ["São Paulo", NAO_ENCONTRADO, "Rio de Janeiro"]


def test_escala_600_mil_linhas(fake_client_cls):
    n, unicos = 600_000, 1_000
    ceps = [f"{10_000_000 + (i % unicos):08d}" for i in range(n)]
    situacao = ["Em uso" if i % 3 else "Inativo" for i in range(n)]
    df = pd.DataFrame({"CEP": ceps, "Situação": situacao})
    client = fake_client_cls(default=lambda c: LookupResult(c, OK, "Cidade", "SP"))
    inicio = time.monotonic()
    out, stats = sanitize(df, "CEP", client, JournalCache(), filter_col="Situação", filter_values=["Em uso"])
    duracao = time.monotonic() - inicio
    assert len(out) == n
    assert len(client.calls) == len(set(client.calls)) <= unicos  # nenhuma consulta repetida
    assert stats.rows_in_scope == sum(1 for s in situacao if s == "Em uso")
    assert duracao < 60, f"processamento local de 600 mil linhas levou {duracao:.1f}s"
