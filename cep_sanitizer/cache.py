"""Cache de resultados definitivos, persistido em disco como JSONL (uma linha por CEP).

Por que um "journal" append-only e não um arquivo reescrito:
* cada resultado é gravado e descarregado (flush) no instante em que chega: se o
  processo morrer, no máximo a última linha fica truncada (e é ignorada ao reler);
* retomar uma execução é só reabrir o arquivo.

Só entram resultados definitivos (OK e NAO_EXISTE). Erros transitórios ficam de fora
para serem tentados de novo na próxima execução.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Iterable, Optional, Union

from .models import DEFINITIVOS, LookupResult

_CEP8 = re.compile(r"^\d{8}$")


class JournalCache:
    def __init__(self, path: Optional[Union[str, Path]] = None, fsync_every: int = 100) -> None:
        self._data: dict[str, LookupResult] = {}
        self._path = Path(path) if path else None
        self._fsync_every = max(1, fsync_every)
        self._fh = None
        self._since_sync = 0
        self.skipped_lines = 0  # linhas corrompidas/ignoradas ao carregar
        if self._path is not None and self._path.exists():
            with self._path.open("r", encoding="utf-8", errors="replace") as fh:
                self._ingest(fh, persist=False)

    # ------------------------------------------------------------------ leitura
    def get(self, cep: str) -> Optional[LookupResult]:
        return self._data.get(cep)

    def __contains__(self, cep: str) -> bool:
        return cep in self._data

    def __len__(self) -> int:
        return len(self._data)

    # ------------------------------------------------------------------ escrita
    def put(self, result: LookupResult) -> bool:
        """Grava um resultado definitivo. Retorna False se o status não é gravável."""
        if result.status not in DEFINITIVOS:
            return False
        self._data[result.cep] = result
        if self._path is not None:
            self._append(result)
        return True

    def merge_jsonl(self, text: str) -> int:
        """Incorpora linhas JSONL (ex.: cache enviado pelo usuário). Retorna quantas entraram."""
        return self._ingest(text.splitlines(), persist=True)

    def to_jsonl(self) -> str:
        return "".join(self._line(r) for r in self._data.values())

    def close(self) -> None:
        if self._fh is not None:
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "JournalCache":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ internos
    @staticmethod
    def _line(result: LookupResult) -> str:
        payload = {
            "cep": result.cep,
            "status": result.status,
            "cidade": result.cidade,
            "uf": result.uf,
        }
        return json.dumps(payload, ensure_ascii=False) + "\n"

    def _ingest(self, lines: Iterable[str], persist: bool) -> int:
        loaded = 0
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
                cep = str(obj["cep"])
                status = str(obj["status"])
                if not _CEP8.match(cep) or status not in DEFINITIVOS:
                    raise ValueError("entrada inválida")
                result = LookupResult(
                    cep, status, str(obj.get("cidade", "")), str(obj.get("uf", ""))
                )
            except (ValueError, KeyError, TypeError):
                self.skipped_lines += 1
                continue
            if persist:
                self.put(result)
            else:
                self._data[cep] = result
            loaded += 1
        return loaded

    def _append(self, result: LookupResult) -> None:
        if self._fh is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            needs_newline = False
            if self._path.exists() and self._path.stat().st_size > 0:
                with self._path.open("rb") as probe:
                    probe.seek(-1, os.SEEK_END)
                    needs_newline = probe.read(1) != b"\n"
            self._fh = self._path.open("a", encoding="utf-8")
            if needs_newline:  # última linha truncada por queda anterior
                self._fh.write("\n")
        self._fh.write(self._line(result))
        self._fh.flush()
        self._since_sync += 1
        if self._since_sync >= self._fsync_every:
            os.fsync(self._fh.fileno())
            self._since_sync = 0
