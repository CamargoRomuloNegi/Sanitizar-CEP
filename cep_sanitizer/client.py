"""Cliente ViaCEP com consultas controladas.

Princípios (o ViaCEP avisa que uso massivo pode bloquear o IP por tempo indeterminado):

* Uma consulta por vez, com intervalo mínimo entre consultas (padrão: 1 s, mais um
  pequeno jitter aleatório que só ACRESCENTA tempo, nunca reduz).
* Falhas transitórias (timeout, conexão, HTTP 5xx) têm poucas novas tentativas, com
  espera crescente (2 s, 4 s, 8 s).
* HTTP 429: respeita o cabeçalho ``Retry-After`` (ou pausa 60 s x ocorrência) e, se
  persistir, ABORTA com ``BlockedError``. HTTP 403: aborta na hora.
* Se vários CEPs seguidos falharem, aborta com ``ServiceUnavailableError`` em vez de
  continuar martelando um serviço fora do ar.

Quem chama (o ``processor``) trata as duas exceções gravando o progresso e parando.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

import requests

from .models import CEP_INVALIDO, ERRO_API, ERRO_REDE, NAO_EXISTE, OK, LookupResult

log = logging.getLogger(__name__)


class BlockedError(RuntimeError):
    """A API indicou limite/bloqueio (HTTP 429 persistente ou 403). Parar e esperar."""


class ServiceUnavailableError(RuntimeError):
    """Muitos CEPs seguidos falharam: serviço ou rede fora do ar. Parar e tentar depois."""


@dataclass
class ClientConfig:
    base_url: str = "https://viacep.com.br/ws"
    min_interval: float = 1.0          # segundos entre consultas
    jitter: float = 0.2                # fração extra aleatória sobre min_interval
    connect_timeout: float = 5.0
    read_timeout: float = 10.0
    max_retries: int = 3               # novas tentativas por CEP em falha transitória
    backoff_base: float = 2.0          # espera = backoff_base ** tentativa
    backoff_max: float = 60.0
    throttle_pause: float = 60.0       # pausa (x nº da ocorrência) em 429 sem Retry-After
    retry_after_max: float = 600.0     # teto para respeitar Retry-After
    max_throttle_events: int = 3       # 429 seguidos até abortar
    max_failed_ceps_in_row: int = 5    # CEPs seguidos com falha até abortar
    user_agent: str = "cep-sanitizer/1.0 (saneamento de base; consultas espaçadas)"


class RateLimiter:
    """Garante intervalo mínimo entre o INÍCIO de consultas consecutivas."""

    def __init__(
        self,
        min_interval: float,
        jitter: float = 0.2,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._min_interval = max(0.0, min_interval)
        self._jitter = max(0.0, jitter)
        self._clock = clock
        self._sleep = sleep
        self._rng = rng
        self._last: Optional[float] = None

    def wait(self) -> None:
        now = self._clock()
        if self._last is not None:
            extra = self._min_interval * self._jitter * self._rng()
            ready_at = self._last + self._min_interval + extra
            if now < ready_at:
                self._sleep(ready_at - now)
        self._last = self._clock()


class ViaCepClient:
    def __init__(
        self,
        config: Optional[ClientConfig] = None,
        session: Optional[Any] = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.cfg = config or ClientConfig()
        self._session = session if session is not None else requests.Session()
        self._sleep = sleep
        self._limiter = RateLimiter(
            self.cfg.min_interval, self.cfg.jitter, clock=clock, sleep=sleep, rng=rng
        )
        self._headers = {"Accept": "application/json", "User-Agent": self.cfg.user_agent}
        self._throttle_events = 0
        self._failed_ceps_in_row = 0
        self.requests_made = 0

    # ------------------------------------------------------------------ API pública
    def lookup(self, cep: str) -> LookupResult:
        """Consulta um CEP de 8 dígitos. Levanta BlockedError/ServiceUnavailableError."""
        url = f"{self.cfg.base_url}/{cep}/json/"
        timeout = (self.cfg.connect_timeout, self.cfg.read_timeout)
        attempt = 0
        last_status, last_detail = ERRO_REDE, "falha desconhecida"

        while True:
            self._limiter.wait()
            self.requests_made += 1
            try:
                resp = self._session.get(url, timeout=timeout, headers=self._headers)
            except requests.RequestException as exc:
                last_status, last_detail = ERRO_REDE, type(exc).__name__
            else:
                code = resp.status_code
                if code == 200:
                    outcome = self._parse_200(cep, resp)
                    if outcome is None:  # corpo não é o JSON esperado: tenta de novo
                        last_status, last_detail = ERRO_API, "resposta não é o JSON esperado"
                    elif outcome.status == ERRO_API:  # JSON válido, mas sem cidade/UF
                        return self._fail(cep, outcome.status, outcome.detalhe)
                    else:
                        self._on_success()
                        return outcome
                elif code == 400:
                    self._on_success()
                    return LookupResult(cep, CEP_INVALIDO, detalhe="HTTP 400 (formato rejeitado pela API)")
                elif code == 429:
                    self._handle_throttle(resp)  # pausa, ou levanta BlockedError
                    continue
                elif code == 403:
                    raise BlockedError(
                        "HTTP 403 do ViaCEP: acesso possivelmente bloqueado. "
                        "Interrompido para não agravar; aguarde antes de retomar."
                    )
                elif code >= 500:
                    last_status, last_detail = ERRO_API, f"HTTP {code}"
                else:
                    return self._fail(cep, ERRO_API, f"HTTP {code}")

            attempt += 1
            if attempt > self.cfg.max_retries:
                return self._fail(cep, last_status, f"{last_detail} (após {attempt} tentativas)")
            self._sleep(self._backoff_delay(attempt))

    # ------------------------------------------------------------------ internos
    @staticmethod
    def _parse_200(cep: str, resp: Any) -> Optional[LookupResult]:
        try:
            data = resp.json()
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        erro = data.get("erro")
        if erro is True or (isinstance(erro, str) and erro.strip().lower() == "true"):
            return LookupResult(cep, NAO_EXISTE, detalhe="CEP não consta na base do ViaCEP")
        cidade = str(data.get("localidade") or "").strip()
        uf = str(data.get("uf") or "").strip().upper()
        if not cidade or not uf:
            return LookupResult(cep, ERRO_API, detalhe="resposta sem localidade/uf")
        return LookupResult(cep, OK, cidade=cidade, uf=uf)

    def _backoff_delay(self, attempt: int) -> float:
        return min(self.cfg.backoff_base ** attempt, self.cfg.backoff_max)

    def _on_success(self) -> None:
        self._throttle_events = 0
        self._failed_ceps_in_row = 0

    def _fail(self, cep: str, status: str, detail: str) -> LookupResult:
        self._failed_ceps_in_row += 1
        if self._failed_ceps_in_row >= self.cfg.max_failed_ceps_in_row:
            raise ServiceUnavailableError(
                f"{self._failed_ceps_in_row} CEPs seguidos falharam (último: {detail}). "
                "Rede ou ViaCEP indisponível; progresso já salvo."
            )
        log.warning("CEP %s falhou: %s - %s", cep, status, detail)
        return LookupResult(cep, status, detalhe=detail)

    def _handle_throttle(self, resp: Any) -> None:
        self._throttle_events += 1
        if self._throttle_events >= self.cfg.max_throttle_events:
            raise BlockedError(
                f"HTTP 429 recebido {self._throttle_events} vezes seguidas: limite ou bloqueio "
                "do ViaCEP. Interrompido; aguarde (possivelmente horas) antes de retomar."
            )
        pause = self._retry_after(resp)
        if pause is None:
            pause = self.cfg.throttle_pause * self._throttle_events
        pause = min(pause, self.cfg.retry_after_max)
        log.warning("HTTP 429; pausando %.0f s antes de tentar de novo", pause)
        self._sleep(pause)

    @staticmethod
    def _retry_after(resp: Any) -> Optional[float]:
        headers = getattr(resp, "headers", None) or {}
        value = headers.get("Retry-After")
        if value is not None and str(value).strip().isdigit():
            return float(str(value).strip())
        return None
