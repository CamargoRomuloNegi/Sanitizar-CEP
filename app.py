"""Interface Streamlit do Sanitizador de CEP.

Pensada para lotes pequenos/médios e para conferência. Para a base completa
(centenas de milhares de linhas) use a linha de comando: ``python -m cep_sanitizer``.

Executar localmente:  streamlit run app.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pandas as pd
import streamlit as st

# Garante que a pasta do projeto esteja no caminho de importação, qualquer que seja
# o diretório de trabalho do Streamlit Cloud.
_RAIZ = Path(__file__).resolve().parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))

if not (_RAIZ / "cep_sanitizer" / "__init__.py").exists():
    st.error(
        "A pasta `cep_sanitizer/` não está no repositório, ao lado do `app.py`. "
        "Envie a pasta inteira (com `__init__.py`, `cache.py`, `cli.py`, `client.py`, "
        "`io_utils.py`, `models.py`, `normalizer.py`, `processor.py`) para o GitHub e reinicie o app."
    )
    st.stop()

from cep_sanitizer.cache import JournalCache
from cep_sanitizer.client import build_client
from cep_sanitizer.io_utils import list_sheets, looks_headerless, read_table, write_table
from cep_sanitizer.processor import (
    COL_CIDADE,
    COL_DIAG,
    COL_STATUS,
    COL_UF,
    build_plan,
    detect_place_columns,
    estimate_work,
    format_duration,
    sanitize,
)

MIME = {
    "csv": "text/csv",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
MODE_INCLUDE = "Consultar só os valores marcados"
MODE_EXCLUDE = "Não consultar os valores marcados"
MODE_NUMERIC = "Consultar só valores numéricos maiores que"
NAMES_DEFAULT = "CEP,Cidade,UF,Bairro,Usado"

st.set_page_config(page_title="Sanitizador de CEP", page_icon="📮", layout="wide")


# ---------------------------------------------------------------------- estado
def _cache() -> JournalCache:
    if "cache" not in st.session_state:
        st.session_state["cache"] = JournalCache()  # em memória; exportável pelo botão
    return st.session_state["cache"]


def _first_index(columns: list, predicate, fallback: int = 0) -> int:
    return next((i for i, c in enumerate(columns) if predicate(str(c).strip().casefold())), fallback)


cache = _cache()

# ---------------------------------------------------------------------- cabeçalho
st.title("Sanitizador de CEP")
st.caption(
    "Lê uma planilha, consulta cada CEP único no ViaCEP e preenche **Cidade Retornada**, "
    "**Estado Retornado** e **Diagnóstico CEP**."
)
st.warning(
    "O ViaCEP avisa que uso massivo para validar bases pode bloquear o acesso por tempo "
    "indeterminado. Este app consulta **uma vez por CEP único**, com intervalo entre consultas, e "
    "**para sozinho** se detectar bloqueio. Para a base completa, use a linha de comando "
    "(`python -m cep_sanitizer`), que grava o progresso em disco e retoma de onde parou."
)

# ---------------------------------------------------------------------- barra lateral
with st.sidebar:
    st.header("Controle de consultas")
    intervalo = st.number_input(
        "Intervalo entre consultas (s)", min_value=0.5, max_value=10.0, value=1.0, step=0.1,
        help="Quanto maior, mais seguro. O piso é 0,5 s para proteger seu acesso.",
    )
    fonte = st.selectbox(
        "Fonte dos CEPs",
        ["viacep-reserva", "viacep", "opencep"],
        format_func={
            "viacep-reserva": "ViaCEP, com OpenCEP de reserva (recomendado)",
            "viacep": "Só ViaCEP",
            "opencep": "Só OpenCEP",
        }.get,
        help="A reserva só entra quando o ViaCEP não responde (ex.: bloqueio do IP do Streamlit Cloud). "
             "Os CEPs resolvidos pela reserva ficam marcados no diagnóstico.",
    )
    max_consultas = st.number_input(
        "Máx. de consultas novas por execução", min_value=1, max_value=5000, value=500, step=50,
        help="Cada clique em 'Iniciar' faz no máximo isso e continua do cache na vez seguinte.",
    )
    st.divider()
    st.header("Progresso salvo")
    st.caption(
        "O Streamlit Cloud não guarda arquivos entre sessões. Baixe o cache ao terminar e "
        "envie-o aqui na próxima vez para não repetir consultas."
    )
    cache_up = st.file_uploader("Retomar de um cache (.jsonl)", type=["jsonl"])
    if cache_up is not None:
        merged = st.session_state.setdefault("merged_caches", set())
        cache_key = (cache_up.name, cache_up.size)
        if cache_key not in merged:
            added = cache.merge_jsonl(cache_up.getvalue().decode("utf-8", errors="replace"))
            merged.add(cache_key)
            st.success(f"{added:,} CEPs carregados do cache.")
    st.metric("CEPs no cache", f"{len(cache):,}")
    if len(cache):
        st.download_button(
            "Baixar cache", cache.to_jsonl().encode("utf-8"),
            file_name="cache_cep.jsonl", mime="application/x-ndjson",
        )

# ---------------------------------------------------------------------- planilha
uploaded = st.file_uploader("Planilha (.csv ou .xlsx)", type=["csv", "xlsx"])
if uploaded is None:
    st.info("Envie uma planilha para começar.")
    st.stop()

data = uploaded.getvalue()
sheet = None
if uploaded.name.lower().endswith(".xlsx"):
    sheets = list_sheets(data)
    sheet = st.selectbox("Aba", sheets) if len(sheets) > 1 else sheets[0]

no_header = st.checkbox(
    "A planilha NÃO tem linha de cabeçalho",
    help="Marque se a primeira linha já é um registro (comum em exportações de banco).",
)
names = None
if no_header:
    names_text = st.text_input("Nomes das colunas (separe por vírgula)", value=NAMES_DEFAULT)
    names = [n.strip() for n in names_text.split(",") if n.strip()]

table_key = (uploaded.name, uploaded.size, sheet, no_header, tuple(names or ()))
if st.session_state.get("table_key") != table_key:
    with st.spinner("Lendo a planilha..."):
        try:
            df, meta = read_table(
                data, uploaded.name, sheet=sheet, header=not no_header, names=names
            )
        except Exception as exc:  # noqa: BLE001 - mostrar a causa ao usuário
            st.error(f"Não consegui ler a planilha: {exc}")
            st.stop()
    st.session_state.update(table_key=table_key, df=df, meta=meta)
    st.session_state.pop("result", None)
    st.session_state.pop("plan_cache", None)

df: pd.DataFrame = st.session_state["df"]
meta = st.session_state["meta"]
columns = list(df.columns)

st.subheader("1. Colunas")
st.write(f"{len(df):,} linhas × {len(columns)} colunas")
if not no_header and looks_headerless(columns):
    st.warning(
        "Os nomes de coluna lidos parecem dados (ex.: só dígitos). Se o arquivo não tem "
        "cabeçalho, marque 'A planilha NÃO tem linha de cabeçalho'; senão o primeiro registro é perdido."
    )
st.dataframe(df.head(5), width="stretch")

cep_col = st.selectbox(
    "Coluna do CEP", columns,
    index=_first_index(columns, lambda c: c == "cep", _first_index(columns, lambda c: "cep" in c)),
)
pad_zero = st.checkbox(
    "Completar com zero à esquerda os CEPs de 7 dígitos",
    value=False,
    help=(
        "Deixe desmarcado (CEP de 7 dígitos = 'Não Encontrado') em bases exportadas de sistemas, onde "
        "CEP curto costuma estar TRUNCADO e completar com zero consultaria o CEP errado. Marque só se a "
        "planilha passou pelo Excel, que apaga o zero inicial."
    ),
)

NO_COMPARE = "(não comparar)"
auto_city, auto_uf = detect_place_columns(columns)
place_options = [NO_COMPARE] + columns
cmp_left, cmp_right = st.columns(2)
city_choice = cmp_left.selectbox(
    "Coluna da Cidade da base (para o Status)", place_options,
    index=place_options.index(auto_city) if auto_city in columns else 0,
)
uf_choice = cmp_right.selectbox(
    "Coluna da UF da base (para o Status)", place_options,
    index=place_options.index(auto_uf) if auto_uf in columns else 0,
)
city_col = None if city_choice == NO_COMPARE else city_choice
uf_col = None if uf_choice == NO_COMPARE else uf_choice
if (city_col is None) != (uf_col is None):
    st.warning("Escolha as duas colunas (Cidade e UF) para gerar o Status, ou nenhuma.")
    st.stop()

use_filter = st.checkbox("Filtrar linhas antes de consultar", value=True)
filter_col, include_values, exclude_values, numeric_gt = None, None, None, None
if use_filter:
    left, right = st.columns(2)
    filter_col = left.selectbox(
        "Coluna do filtro", columns,
        index=_first_index(columns, lambda c: any(k in c for k in ("usado", "situa", "uso", "status", "ativo"))),
    )
    mode = right.radio("Regra", [MODE_NUMERIC, MODE_INCLUDE, MODE_EXCLUDE], horizontal=False)
    if mode == MODE_NUMERIC:
        numeric_gt = st.number_input(
            f"Consultar só linhas com '{filter_col}' maior que", value=0.0, step=1.0,
            help="Linhas com valor igual ou menor (ex.: 0), vazio ou não numérico ficam fora.",
        )
    else:
        uniques = sorted(df[filter_col].astype(str).unique())
        default_text = "0" if mode == MODE_EXCLUDE else "Em uso"
        if len(uniques) <= 50:
            default = [v for v in uniques if v.strip().casefold() == default_text.casefold()]
            picked = st.multiselect("Valores", uniques, default=default)
        else:
            raw = st.text_input("Valores (separe por ;)", value=default_text)
            picked = [v.strip() for v in raw.split(";") if v.strip()]
        if not picked:
            st.error("Escolha ao menos um valor para o filtro, ou desmarque a opção.")
            st.stop()
        if mode == MODE_INCLUDE:
            include_values = picked
        else:
            exclude_values = picked

# ---------------------------------------------------------------------- plano
plan_key = (
    table_key, cep_col, filter_col, tuple(include_values or ()),
    tuple(exclude_values or ()), numeric_gt, pad_zero,
)
plan_cache = st.session_state.setdefault("plan_cache", {})
if plan_key not in plan_cache:
    plan_cache.clear()
    with st.spinner("Analisando CEPs..."):
        try:
            plan_cache[plan_key] = build_plan(
                df, cep_col, filter_col, include_values,
                exclude_values=exclude_values, numeric_gt=numeric_gt, pad_leading_zero=pad_zero,
            )
        except (KeyError, ValueError) as exc:
            st.error(str(exc))
            st.stop()
plan = plan_cache[plan_key]
work = estimate_work(plan, cache)

st.subheader("2. Estimativa")
c1, c2, c3, c4 = st.columns(4)
c1.metric("Linhas a tratar", f"{work['rows_in_scope']:,}", help=f"{work['rows_out_of_scope']:,} fora do filtro")
c2.metric("CEPs únicos válidos", f"{work['unique_ceps']:,}")
c3.metric("Já no cache", f"{work['cached']:,}")
c4.metric("A consultar", f"{work['to_query']:,}")
if plan.filter_non_numeric:
    st.warning(
        f"{plan.filter_non_numeric:,} linhas têm valor vazio/não numérico em '{filter_col}' e ficaram fora do escopo."
    )

this_run = min(work["to_query"], int(max_consultas))
per_query = float(intervalo) * 1.1 + 0.3
st.write(
    f"Nesta execução: **{this_run:,}** consultas, cerca de **{format_duration(this_run * per_query)}**. "
    f"Total restante: ~{format_duration(work['to_query'] * per_query)}."
)
if work["to_query"] > 5000:
    st.warning("Volume grande para o navegador: prefira a linha de comando para esta base.")

# ---------------------------------------------------------------------- execução
st.subheader("3. Executar")
st.caption("Não mexa nos controles da página enquanto roda: isso interrompe a execução (o que já foi consultado fica no cache).")
if st.button("Iniciar consultas", type="primary"):
    client = build_client(fonte, float(intervalo))
    bar = st.progress(0.0, text="Consultando CEPs...")
    started = time.monotonic()

    def on_progress(done: int, total: int, _result) -> None:
        if done % 5 == 0 or done == total:
            remaining = (time.monotonic() - started) / done * (total - done)
            bar.progress(done / total, text=f"{done:,}/{total:,} consultas | restante ~{format_duration(remaining)}")

    out, stats = sanitize(
        df, cep_col, client, cache,
        city_col=city_col, uf_col=uf_col,
        max_new_lookups=int(max_consultas), progress=on_progress, plan=plan,
    )
    bar.empty()
    stem, dot, ext = uploaded.name.rpartition(".")
    st.session_state["result"] = {
        "data": write_table(out, meta),
        "name": f"{stem}_sanitizado.{ext}",
        "mime": MIME[meta.kind],
        "stats": stats,
        "preview": out.head(200),
    }
    st.rerun()  # atualiza as métricas da estimativa com o cache novo

result = st.session_state.get("result")
if result:
    stats = result["stats"]
    st.subheader("4. Resultado")
    if stats.aborted_reason:
        st.error(f"Execução interrompida: {stats.aborted_reason}")
    elif stats.pending_ceps:
        st.info(f"{stats.pending_ceps:,} CEPs únicos ainda pendentes. Clique em 'Iniciar' de novo para continuar.")
    else:
        st.success("Todos os CEPs do escopo foram tratados.")
    st.write(f"Consultas novas na última execução: **{stats.queried:,}**.")
    tables = st.columns(2)
    if stats.status_counts:
        tables[0].write("Linhas por Status")
        tables[0].dataframe(
            pd.DataFrame(sorted(stats.status_counts.items()), columns=[COL_STATUS, "Linhas"]),
            hide_index=True,
        )
    tables[1].write("Linhas por diagnóstico")
    tables[1].dataframe(
        pd.DataFrame(sorted(stats.diag_counts.items()), columns=["Diagnóstico", "Linhas"]),
        hide_index=True,
    )
    st.download_button(
        "Baixar planilha preenchida", result["data"], file_name=result["name"], mime=result["mime"]
    )
    st.caption(f"Pré-visualização (200 primeiras linhas). Colunas novas: {COL_CIDADE}, {COL_UF}, {COL_DIAG}.")
    st.dataframe(result["preview"], width="stretch")
