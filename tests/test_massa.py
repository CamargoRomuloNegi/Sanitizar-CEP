"""Massa de 1.000 CEPs: composição, reprodutibilidade e execução ponta a ponta com API simulada."""

from pathlib import Path

import pandas as pd
import pytest

from cep_sanitizer.cache import JournalCache
from cep_sanitizer.io_utils import read_table
from cep_sanitizer.models import (
    OK,
    STATUS_NAO_ENCONTRADO,
    STATUS_OK,
    STATUS_REVER,
    LookupResult,
)
from cep_sanitizer.normalizer import normalize_cep
from cep_sanitizer.processor import COL_CIDADE, COL_DIAG, COL_STATUS, COL_UF, sanitize
from tools.gerar_massa_teste import ANCORAS, NOMES, gerar
from tools.validar_resultado import validar

from conftest import FakeClient

RAIZ = Path(__file__).resolve().parent.parent
ESPERADO = {
    "ANCORA_LIMPO": 50, "ANCORA_HIFEN": 30, "ANCORA_PONTO_HIFEN": 20, "ANCORA_ESPACOS": 20,
    "ANCORA_FLOAT_EXCEL": 10, "ANCORA_CIDADE_DIVERGENTE": 20, "ANCORA_UF_DIVERGENTE": 20,
    "CEP_VAZIO": 30, "CEP_5_DIGITOS": 30, "CEP_7_DIGITOS": 30, "CEP_INVALIDO": 90,
    "NAO_EXISTE_DOC": 20, "NAO_EXISTE_PROVAVEL": 40, "EXPLORATORIO": 290, "FORA_DO_FILTRO": 300,
}
API_SIMULADA = {cep: (cidade, uf) for cep, cidade, uf, _ in ANCORAS}


@pytest.fixture(scope="module")
def massa():
    return gerar(2026)


def _rodar(massa, tabela=None, **kw):
    m, g = massa
    client = FakeClient(tabela if tabela is not None else API_SIMULADA, **kw)
    out, stats = sanitize(
        m, "CEP", client, JournalCache(),
        filter_col="Usado", numeric_gt=0, pad_leading_zero=False, city_col="Cidade", uf_col="UF",
    )
    return m, g, out, stats, client


def test_composicao_no_layout_da_base_real(massa):
    m, g = massa
    assert len(m) == len(g) == 1000
    assert g["cenario"].value_counts().to_dict() == ESPERADO
    assert list(m.columns) == NOMES == ["CEP", "Cidade", "UF", "Bairro", "Usado"]
    usado = pd.to_numeric(m["Usado"])
    assert (usado[g["cenario"] == "FORA_DO_FILTRO"] == 0).all()
    assert (usado[g["cenario"] != "FORA_DO_FILTRO"] > 0).all()
    assert (usado == 0).sum() == 300


def test_reprodutivel_e_arquivo_versionado_igual(massa):
    m, g = massa
    pd.testing.assert_frame_equal(m, gerar(2026)[0])
    pd.testing.assert_frame_equal(g, gerar(2026)[1])
    arq_m, _ = read_table(RAIZ / "dados_exemplo" / "massa_teste_1000.csv", header=False, names=NOMES)
    arq_g, _ = read_table(RAIZ / "dados_exemplo" / "massa_teste_1000_gabarito.csv")
    pd.testing.assert_frame_equal(arq_m, m)
    pd.testing.assert_frame_equal(arq_g, g)


def test_arquivo_versionado_nao_tem_cabecalho():
    primeira = (RAIZ / "dados_exemplo" / "massa_teste_1000.csv").read_text(encoding="utf-8-sig").splitlines()[0]
    assert primeira.count(";") == 4 and not primeira.startswith("CEP;")


def test_ponta_a_ponta_aprovado_sem_fail_nem_revisar(massa):
    _, g, out, stats, _ = _rodar(massa)
    rel = validar(out, g)
    assert rel.por_nivel.get("FAIL", 0) == 0, rel.achados[:5]
    assert rel.por_nivel.get("REVISAR", 0) == 0, rel.achados[:5]
    assert rel.por_nivel["PASS"] == 1000
    assert stats.complete


def test_contagem_de_status_bate_com_a_composicao(massa):
    _, _, _, stats, _ = _rodar(massa)
    ok = 50 + 30 + 20 + 20 + 10
    assert stats.status_counts == {
        STATUS_OK: ok,
        STATUS_REVER: 40,                                # 20 cidades + 20 UFs divergentes
        STATUS_NAO_ENCONTRADO: 30 + 30 + 30 + 90 + 20 + 40 + 290,
    }
    assert sum(stats.status_counts.values()) == 700      # as 300 com Usado = 0 ficam sem Status


def test_so_consulta_ceps_unicos_das_linhas_usadas(massa):
    m, _, _, stats, client = _rodar(massa)
    usadas = m[pd.to_numeric(m["Usado"]) > 0]
    esperados = {n.cep for n in (normalize_cep(c, pad_leading_zero=False) for c in usadas["CEP"]) if n.status == OK}
    assert set(client.calls) == esperados
    assert len(client.calls) == len(set(client.calls))  # nenhuma repetida
    assert len(client.calls) == stats.unique_ceps
    # 700 linhas com Usado > 0 viram ~320 consultas: 290 exploratórios (únicos por construção)
    # + as âncoras e os "não existe" repetidos, que a deduplicação colapsa.
    assert stats.rows_in_scope == 700
    assert len(client.calls) < 0.5 * stats.rows_in_scope


def test_linhas_usado_zero_ficam_em_branco_mesmo_com_cep_consultado_em_outra_linha(massa):
    m, g, out, _, client = _rodar(massa)
    fora = g["cenario"] == "FORA_DO_FILTRO"
    assert (out.loc[fora, [COL_CIDADE, COL_UF, COL_STATUS]] == "").all().all()
    assert (out.loc[fora, COL_DIAG] == "FORA_DO_FILTRO").all()
    ancora_zero = m.loc[fora & m["CEP"].isin(API_SIMULADA), "CEP"]
    assert len(ancora_zero) > 0 and set(ancora_zero) <= set(client.calls)  # consultado, mas NÃO nesta linha


def test_regras_do_escopo_branco_5_e_7_digitos(massa):
    _, g, out, _, _ = _rodar(massa)
    for cenario, codigo in [("CEP_VAZIO", "CEP_VAZIO"), ("CEP_5_DIGITOS", "CEP_INVALIDO"), ("CEP_7_DIGITOS", "CEP_INVALIDO")]:
        linhas = out[g["cenario"] == cenario]
        assert (linhas[COL_CIDADE] == "não encontrado").all()
        assert (linhas[COL_UF] == "não encontrado").all()
        assert (linhas[COL_STATUS] == STATUS_NAO_ENCONTRADO).all()
        assert (linhas[COL_DIAG].str.split(" - ").str[0] == codigo).all()


def test_original_intacto_e_arquivo_completo(massa):
    m, g, out, _, _ = _rodar(massa)
    assert len(out) == len(m)
    assert out[NOMES].equals(m)
    assert list(out.columns) == NOMES + [COL_CIDADE, COL_UF, COL_STATUS, COL_DIAG]


# ---------------------------------------------------------------- o validador também precisa funcionar
def test_validador_pega_status_errado_em_cenario_verificado(massa):
    _, g, out, _, _ = _rodar(massa)
    linha = g.index[(g["cenario"] == "ANCORA_LIMPO") & (g["confianca"] == "verificado")][0]
    out = out.copy()
    out.loc[linha, COL_STATUS] = STATUS_REVER
    assert validar(out, g).falhou


def test_validador_pega_cidade_errada_em_cenario_verificado(massa):
    _, g, out, _, _ = _rodar(massa)
    linha = g.index[(g["cenario"] == "ANCORA_LIMPO") & (g["confianca"] == "verificado")][0]
    out = out.copy()
    out.loc[linha, COL_CIDADE] = "Campinas"
    assert validar(out, g).falhou


def test_validador_so_pede_revisao_quando_gabarito_e_provavel(massa):
    # Caso realista: o ViaCEP devolve outra cidade do que o gabarito "provável" (de memória) previa.
    _, g, out, _, _ = _rodar(massa)
    linha = g.index[(g["cenario"] == "ANCORA_LIMPO") & (g["confianca"] == "provavel")][0]
    g = g.copy()
    g.loc[linha, "esperado_cidade"] = "Campinas"
    rel = validar(out, g)
    assert rel.por_nivel["REVISAR"] == 1 and rel.por_nivel.get("FAIL", 0) == 0


def test_validador_reprova_quando_o_gabarito_verificado_diverge(massa):
    _, g, out, _, _ = _rodar(massa)
    linha = g.index[(g["cenario"] == "ANCORA_LIMPO") & (g["confianca"] == "verificado")][0]
    g = g.copy()
    g.loc[linha, "esperado_cidade"] = "Campinas"
    assert validar(out, g).falhou


def test_validador_pega_cep_original_alterado(massa):
    _, g, out, _, _ = _rodar(massa)
    out = out.copy()
    out.loc[0, "CEP"] = "alterado"
    assert validar(out, g).falhou


def test_validador_pega_invalido_que_virou_ok(massa):
    _, g, out, _, _ = _rodar(massa)
    linha = g.index[g["cenario"] == "CEP_INVALIDO"][0]
    out = out.copy()
    out.loc[linha, [COL_CIDADE, COL_UF, COL_STATUS, COL_DIAG]] = ["São Paulo", "SP", STATUS_OK, "OK"]
    assert validar(out, g).falhou


def test_validador_pega_fora_do_filtro_com_status_preenchido(massa):
    _, g, out, _, _ = _rodar(massa)
    linha = g.index[g["cenario"] == "FORA_DO_FILTRO"][0]
    out = out.copy()
    out.loc[linha, COL_STATUS] = STATUS_NAO_ENCONTRADO
    assert validar(out, g).falhou


def test_validador_sinaliza_uf_fora_da_faixa(massa):
    # API simulada devolve sempre RS: CEPs de outras faixas viram REVISAR (nunca FAIL)
    m, g = massa
    client = FakeClient(default=lambda c: LookupResult(c, OK, "Porto Alegre", "RS"))
    out, _ = sanitize(
        m, "CEP", client, JournalCache(), filter_col="Usado", numeric_gt=0,
        pad_leading_zero=False, city_col="Cidade", uf_col="UF",
    )
    exploratorio = validar(out, g).por_cenario["EXPLORATORIO"]
    assert exploratorio.get("REVISAR", 0) > 0 and exploratorio.get("FAIL", 0) == 0


def test_validador_exige_mesmo_numero_de_linhas(massa):
    _, g, out, _, _ = _rodar(massa)
    with pytest.raises(ValueError):
        validar(out.head(10), g)
