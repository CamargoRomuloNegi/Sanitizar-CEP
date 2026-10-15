"""Plano de trabalho e preenchimento do DataFrame.

Fluxo de ``sanitize``:

1. Aplica o filtro opcional (ex.: coluna "Usado" maior que zero). Linhas fora do filtro
   NÃO são consultadas e ficam com cidade/UF/Status em branco e diagnóstico FORA_DO_FILTRO.
2. Normaliza os CEPs das linhas no escopo e reduz a CEPs ÚNICOS (cada um é consultado
   no máximo uma vez, mesmo que apareça em milhares de linhas).
3. Consulta só o que não está no cache, em ordem de aparição, respeitando um limite
   opcional de consultas novas por execução.
4. Se a API indicar bloqueio/indisponibilidade, ou o usuário interromper, para na hora:
   o que já foi obtido está no cache e o restante fica como PENDENTE.
5. Espalha os resultados de volta para as linhas (mapeamento por dicionário, rápido
   mesmo com 600 mil linhas).
6. Se informadas as colunas de Cidade e UF da base, compara com o retorno do ViaCEP e
   preenche a coluna Status (veja ``models.STATUS_*``).
"""

from __future__ import annotations

import re
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

import pandas as pd

from .cache import JournalCache
from .client import BlockedError, ServiceUnavailableError
from .models import (
    CEP_INVALIDO,
    CEP_VAZIO,
    ERRO_API,
    ERRO_REDE,
    FORA_DO_FILTRO,
    NAO_ENCONTRADO,
    NAO_EXISTE,
    OK,
    PENDENTE,
    STATUS_NAO_ENCONTRADO,
    STATUS_OK,
    STATUS_REVER,
    LookupResult,
)
from .normalizer import NormalizedCep, normalize_cep

COL_CIDADE = "Cidade Retornada"
COL_UF = "Estado Retornado"
COL_STATUS = "Status"
COL_DIAG = "Diagnóstico CEP"

_CITY_NAMES = ("cidade", "municipio", "município", "localidade")
_UF_NAMES = ("uf", "estado", "sigla uf", "sigla_uf")
_NOT_FOUND_CODES = frozenset({NAO_EXISTE, CEP_INVALIDO, CEP_VAZIO, ERRO_REDE, ERRO_API})


# ---------------------------------------------------------------------- utilidades
def format_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h {minutes}min"
    if hours:
        return f"{hours}h {minutes}min"
    if minutes:
        return f"{minutes}min {secs}s"
    return f"{secs}s"


def _strip_accents(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value))
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def _norm_text(value: object) -> str:
    """Minúsculas, sem acentos, espaços colapsados: 'EM  uso ' == 'em uso'."""
    return " ".join(_strip_accents(value).casefold().split())


def _norm_place(value: object) -> str:
    """Nome de lugar comparável: sem acento, sem caixa, pontuação vira espaço.

    'Sao Paulo   ', 'SÃO PAULO' e 'São Paulo' ficam iguais; "Ji-Parana" == "Ji-Paraná";
    "Santa Barbara d'Oeste" == "Santa Bárbara D Oeste".
    """
    return re.sub(r"[^0-9a-z]+", " ", _strip_accents(value).casefold()).strip()


def detect_place_columns(columns) -> tuple[Optional[str], Optional[str]]:
    """Procura, pelo nome, as colunas de Cidade e de UF da base. Devolve (cidade, uf)."""
    lowered = [(c, str(c).strip().casefold()) for c in columns]
    city = next((c for c, low in lowered if low in _CITY_NAMES), None)
    uf = next((c for c, low in lowered if low in _UF_NAMES), None)
    return city, uf


def _ensure_range_index(df: pd.DataFrame) -> pd.DataFrame:
    if not df.index.equals(pd.RangeIndex(len(df))):
        return df.reset_index(drop=True)
    return df


# ---------------------------------------------------------------------- plano
@dataclass
class Plan:
    mask: pd.Series                       # True = linha no escopo (será consultada)
    raw_in_scope: pd.Series               # valor original do CEP das linhas no escopo
    norm_map: dict[str, NormalizedCep]    # valor original -> CEP normalizado
    ceps: list[str]                       # CEPs únicos válidos, em ordem de aparição
    rows_total: int
    rows_in_scope: int
    filter_non_numeric: int = 0           # linhas fora do escopo por valor não numérico (filtro numérico)


def build_plan(
    df: pd.DataFrame,
    cep_col: str,
    filter_col: Optional[str] = None,
    filter_values: Optional[Iterable[str]] = None,
    *,
    exclude_values: Optional[Iterable[str]] = None,
    numeric_gt: Optional[float] = None,
    pad_leading_zero: bool = False,
) -> Plan:
    """Define o que será consultado.

    Com ``filter_col``, informe EXATAMENTE uma regra:
      * ``filter_values``  consulta só linhas cujo valor está na lista (ex.: "Em uso");
      * ``exclude_values`` consulta tudo, exceto linhas cujo valor está na lista (ex.: "0");
      * ``numeric_gt``     consulta só linhas cujo valor numérico é MAIOR que N (ex.: 0).
        Valor vazio ou não numérico fica fora e é contado em ``filter_non_numeric``.
    """
    df = _ensure_range_index(df)
    if cep_col not in df.columns:
        raise KeyError(f"Coluna de CEP não encontrada: {cep_col!r}")

    rules = [filter_values is not None, exclude_values is not None, numeric_gt is not None]
    non_numeric = 0
    if filter_col:
        if filter_col not in df.columns:
            raise KeyError(f"Coluna do filtro não encontrada: {filter_col!r}")
        if sum(rules) != 1:
            raise ValueError(
                "Informe exatamente uma regra de filtro: incluir valores, excluir valores ou maior que."
            )
        column = df[filter_col].fillna("").astype(str)
        if numeric_gt is not None:
            numbers = pd.to_numeric(column.str.strip().str.replace(",", ".", regex=False), errors="coerce")
            non_numeric = int(numbers.isna().sum())
            mask = (numbers > numeric_gt).fillna(False)
        else:
            values = filter_values if filter_values is not None else exclude_values
            listed = {_norm_text(v) for v in values if str(v).strip()}
            if not listed:
                raise ValueError("Informe ao menos um valor para o filtro (ex.: 'Em uso').")
            matches = column.map(_norm_text).isin(listed)
            mask = matches if filter_values is not None else ~matches
    elif any(rules):
        raise ValueError("Informe a coluna do filtro.")
    else:
        mask = pd.Series(True, index=df.index)

    raw = df.loc[mask, cep_col].fillna("").astype(str)
    norm_map: dict[str, NormalizedCep] = {}
    ordered: dict[str, None] = {}
    for value in raw.drop_duplicates():
        normalized = normalize_cep(value, pad_leading_zero)
        norm_map[value] = normalized
        if normalized.status == OK and normalized.cep is not None:
            ordered.setdefault(normalized.cep, None)

    return Plan(
        mask=mask,
        raw_in_scope=raw,
        norm_map=norm_map,
        ceps=list(ordered),
        rows_total=len(df),
        rows_in_scope=int(mask.sum()),
        filter_non_numeric=non_numeric,
    )


def estimate_work(plan: Plan, cache: JournalCache) -> dict:
    """Quanto trabalho falta: CEPs únicos, quantos já estão no cache e quantos consultar."""
    cached = sum(1 for cep in plan.ceps if cep in cache)
    return {
        "rows_total": plan.rows_total,
        "rows_in_scope": plan.rows_in_scope,
        "rows_out_of_scope": plan.rows_total - plan.rows_in_scope,
        "unique_ceps": len(plan.ceps),
        "cached": cached,
        "to_query": len(plan.ceps) - cached,
    }


# ---------------------------------------------------------------------- execução
@dataclass
class SanitizeStats:
    rows_total: int = 0
    rows_in_scope: int = 0
    unique_ceps: int = 0
    from_cache: int = 0
    queried: int = 0                      # consultas novas feitas nesta execução
    pending_ceps: int = 0                 # CEPs únicos ainda não consultados
    aborted_reason: Optional[str] = None  # bloqueio, indisponibilidade ou interrupção
    elapsed: float = 0.0
    diag_counts: dict = field(default_factory=dict)    # linhas por código de diagnóstico
    status_counts: dict = field(default_factory=dict)  # linhas por Status (se houve comparação)

    @property
    def complete(self) -> bool:
        return self.aborted_reason is None and self.pending_ceps == 0


ProgressFn = Callable[[int, int, LookupResult], None]


def _row_info(n: NormalizedCep, r: Optional[LookupResult]) -> tuple[str, str, str]:
    if n.status == CEP_VAZIO:
        return NAO_ENCONTRADO, NAO_ENCONTRADO, f"{CEP_VAZIO} - CEP em branco"
    if n.status == CEP_INVALIDO:
        return NAO_ENCONTRADO, NAO_ENCONTRADO, f"{CEP_INVALIDO} - {n.detalhe}"
    if r is None:
        return "", "", f"{PENDENTE} - não consultado (limite da execução ou interrupção)"
    if r.status == OK:
        note = f" - CEP completado com zero à esquerda (consultado: {n.cep})" if n.padded else ""
        return r.cidade, r.uf, f"{OK}{note}"
    if r.status == NAO_EXISTE:
        return NAO_ENCONTRADO, NAO_ENCONTRADO, f"{NAO_EXISTE} - {r.detalhe}"
    return NAO_ENCONTRADO, NAO_ENCONTRADO, f"{r.status} - {r.detalhe}"


def _with_note(diag: str, note: str) -> str:
    return f"{diag} | {note}" if " - " in diag else f"{diag} - {note}"


def _fill_status(out: pd.DataFrame, codes: pd.Series, city_col: str, uf_col: str) -> None:
    """Preenche COL_STATUS e anota a divergência no diagnóstico (só linhas com CEP encontrado)."""
    status = pd.Series("", index=out.index)
    status[codes.isin(_NOT_FOUND_CODES)] = STATUS_NAO_ENCONTRADO

    found = codes.eq(OK)
    if found.any():
        base_city_raw = out.loc[found, city_col].fillna("").astype(str).str.strip()
        base_uf = out.loc[found, uf_col].fillna("").astype(str).str.strip().str.upper()
        ret_city = out.loc[found, COL_CIDADE]
        ret_uf = out.loc[found, COL_UF].str.strip().str.upper()

        base_city = base_city_raw.map(_norm_place)
        city_ok = (base_city == ret_city.map(_norm_place)) & (base_city != "")
        uf_ok = base_uf == ret_uf
        both_ok = city_ok & uf_ok
        status.loc[found] = both_ok.map({True: STATUS_OK, False: STATUS_REVER})

        for idx in both_ok.index[~both_ok.to_numpy()]:  # só as divergências (poucas)
            parts = []
            if not city_ok[idx]:
                parts.append(f"cidade base '{base_city_raw[idx]}' x ViaCEP '{ret_city[idx]}'")
            if not uf_ok[idx]:
                parts.append(f"UF base '{base_uf[idx]}' x ViaCEP '{ret_uf[idx]}'")
            out.at[idx, COL_DIAG] = _with_note(out.at[idx, COL_DIAG], "difere da base: " + "; ".join(parts))
    out[COL_STATUS] = status


def sanitize(
    df: pd.DataFrame,
    cep_col: str,
    client,
    cache: JournalCache,
    *,
    filter_col: Optional[str] = None,
    filter_values: Optional[Iterable[str]] = None,
    exclude_values: Optional[Iterable[str]] = None,
    numeric_gt: Optional[float] = None,
    pad_leading_zero: bool = False,
    city_col: Optional[str] = None,
    uf_col: Optional[str] = None,
    max_new_lookups: Optional[int] = None,
    progress: Optional[ProgressFn] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    plan: Optional[Plan] = None,
) -> tuple[pd.DataFrame, SanitizeStats]:
    """Preenche Cidade/Estado (ViaCEP), Status (se houver comparação) e Diagnóstico.

    Não altera ``df``; devolve uma cópia com TODAS as linhas. Informe ``city_col`` e
    ``uf_col`` (as colunas da base) para gerar a coluna Status. Os argumentos de filtro e
    ``pad_leading_zero`` só valem quando ``plan`` não é informado (veja ``build_plan``).
    """
    started = time.monotonic()
    df = _ensure_range_index(df)
    if (city_col is None) != (uf_col is None):
        raise ValueError("Para comparar, informe as duas colunas da base: Cidade e UF.")
    for name in (city_col, uf_col):
        if name is not None and name not in df.columns:
            raise KeyError(f"Coluna da base não encontrada: {name!r}")
    plan = plan or build_plan(
        df, cep_col, filter_col, filter_values,
        exclude_values=exclude_values, numeric_gt=numeric_gt, pad_leading_zero=pad_leading_zero,
    )
    stats = SanitizeStats(
        rows_total=plan.rows_total,
        rows_in_scope=plan.rows_in_scope,
        unique_ceps=len(plan.ceps),
    )

    results: dict[str, LookupResult] = {}
    to_query: list[str] = []
    for cep in plan.ceps:
        cached = cache.get(cep)
        if cached is not None:
            results[cep] = cached
            stats.from_cache += 1
        else:
            to_query.append(cep)

    budget = len(to_query) if max_new_lookups is None else min(len(to_query), max(0, max_new_lookups))
    done = 0
    for cep in to_query:
        if done >= budget:
            break
        if should_stop is not None and should_stop():
            stats.aborted_reason = "interrompido pelo usuário"
            break
        try:
            result = client.lookup(cep)
        except BlockedError as exc:
            stats.aborted_reason = f"possível bloqueio do ViaCEP: {exc}"
            break
        except ServiceUnavailableError as exc:
            stats.aborted_reason = f"serviço indisponível: {exc}"
            break
        cache.put(result)  # só grava se for definitivo
        results[cep] = result
        done += 1
        if progress is not None:
            progress(done, budget, result)
    stats.queried = done
    stats.pending_ceps = len(to_query) - done

    # ---- espalha os resultados para as linhas
    info = {
        raw: _row_info(n, results.get(n.cep) if n.cep else None)
        for raw, n in plan.norm_map.items()
    }
    out = df.copy()
    out[COL_CIDADE] = ""
    out[COL_UF] = ""
    if city_col is not None:
        out[COL_STATUS] = ""  # reserva a posição (antes do diagnóstico); preenchida abaixo
    out[COL_DIAG] = FORA_DO_FILTRO
    raw = plan.raw_in_scope
    out.loc[plan.mask, COL_CIDADE] = raw.map({k: v[0] for k, v in info.items()})
    out.loc[plan.mask, COL_UF] = raw.map({k: v[1] for k, v in info.items()})
    out.loc[plan.mask, COL_DIAG] = raw.map({k: v[2] for k, v in info.items()})

    codes = out[COL_DIAG].str.split(" - ").str[0]
    if city_col is not None:
        _fill_status(out, codes, city_col, uf_col)
        stats.status_counts = dict(Counter(s for s in out[COL_STATUS] if s))
    stats.diag_counts = dict(Counter(codes))
    stats.elapsed = time.monotonic() - started
    return out, stats
