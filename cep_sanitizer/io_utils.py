"""Leitura e escrita de planilhas (CSV e XLSX).

Tudo é lido como TEXTO (``dtype=str``) e sem converter vazios em NaN: assim o CEP
``01001000`` nunca vira ``1001000`` e as demais colunas voltam idênticas à entrada.

Arquivos sem linha de cabeçalho (comum em exportações de banco) são lidos com
``header=False`` e ``names=[...]``: sem isso, o primeiro registro seria engolido como
nome de coluna.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Union

import pandas as pd

Source = Union[str, Path, bytes]

_ENCODINGS = ("utf-8-sig", "cp1252", "latin-1")
_SEPARATORS = (";", ",", "\t", "|")


@dataclass
class TableMeta:
    kind: str = "csv"                  # "csv" | "xlsx"
    sep: str = ";"
    encoding: str = "utf-8-sig"
    sheet: Optional[str] = None


def _is_bytes(source: Source) -> bool:
    return isinstance(source, (bytes, bytearray))


def _head(source: Source, size: int = 65536) -> bytes:
    if _is_bytes(source):
        return bytes(source[:size])
    with open(source, "rb") as fh:
        return fh.read(size)


def detect_separator(head: bytes) -> str:
    """Escolhe o separador mais frequente na primeira linha (padrão: ';')."""
    first_line = head.decode("latin-1").splitlines()[0] if head else ""
    counts = {sep: first_line.count(sep) for sep in _SEPARATORS}
    best = max(counts, key=counts.get)
    return best if counts[best] > 0 else ";"


def looks_headerless(columns) -> bool:
    """Heurística: nome de coluna só com dígitos sugere que a 1ª linha é um registro."""
    return any(re.fullmatch(r"\s*\d+\s*", str(c)) for c in columns)


def list_sheets(data: bytes) -> list[str]:
    return pd.ExcelFile(io.BytesIO(data), engine="openpyxl").sheet_names


def _apply_names(df: pd.DataFrame, header: bool, names: Optional[Sequence[str]]) -> pd.DataFrame:
    if header:
        return df
    n_cols = df.shape[1]
    columns = list(names) if names is not None else [f"Coluna{i + 1}" for i in range(n_cols)]
    if len(columns) != n_cols:
        raise ValueError(
            f"O arquivo tem {n_cols} colunas, mas foram informados {len(columns)} nomes: {columns}"
        )
    df.columns = columns
    return df


def read_table(
    source: Source,
    filename: Optional[str] = None,
    sheet: Optional[str] = None,
    sep: Optional[str] = None,
    encoding: Optional[str] = None,
    header: bool = True,
    names: Optional[Sequence[str]] = None,
) -> tuple[pd.DataFrame, TableMeta]:
    """Lê CSV/XLSX como texto. ``source`` é caminho ou bytes (então informe ``filename``).

    ``header=False`` trata a primeira linha como registro; ``names`` dá os nomes das colunas.
    """
    name = (filename or ("" if _is_bytes(source) else str(source))).lower()
    header_arg = 0 if header else None

    if name.endswith((".xlsx", ".xlsm")):
        buf = io.BytesIO(source) if _is_bytes(source) else source
        df = pd.read_excel(
            buf,
            sheet_name=sheet if sheet is not None else 0,
            header=header_arg,
            dtype=str,
            keep_default_na=False,
            engine="openpyxl",
        )
        return _apply_names(df, header, names), TableMeta(kind="xlsx", sheet=sheet)

    if not name.endswith((".csv", ".txt", ".tsv")):
        raise ValueError("Formato não suportado. Use .csv ou .xlsx.")

    sep = sep or detect_separator(_head(source))
    candidates = [encoding] if encoding else list(_ENCODINGS)
    last_error: Optional[Exception] = None
    for enc in candidates:
        try:
            buf = io.BytesIO(source) if _is_bytes(source) else source
            df = pd.read_csv(
                buf, sep=sep, encoding=enc, header=header_arg, dtype=str, keep_default_na=False
            )
            return _apply_names(df, header, names), TableMeta(kind="csv", sep=sep, encoding=enc)
        except UnicodeDecodeError as exc:
            last_error = exc
    raise ValueError(f"Não foi possível decodificar o CSV ({candidates}).") from last_error


def write_table(df: pd.DataFrame, meta: TableMeta) -> bytes:
    """Serializa para bytes (usado pelo Streamlit)."""
    if meta.kind == "xlsx":
        buf = io.BytesIO()
        df.to_excel(buf, index=False, engine="openpyxl", sheet_name=meta.sheet or "Resultado")
        return buf.getvalue()
    text = df.to_csv(index=False, sep=meta.sep)
    return text.encode(meta.encoding, errors="replace")


def write_table_to_path(df: pd.DataFrame, meta: TableMeta, path: Union[str, Path]) -> None:
    """Grava direto em disco (usado pela CLI; evita duplicar 600 mil linhas em memória)."""
    if meta.kind == "xlsx":
        df.to_excel(path, index=False, engine="openpyxl", sheet_name=meta.sheet or "Resultado")
    else:
        df.to_csv(path, index=False, sep=meta.sep, encoding=meta.encoding, errors="replace")
