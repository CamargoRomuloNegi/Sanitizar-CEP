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
    nome: str = "ViaCEP"                       # nome da fonte (aparece em mensagens e no diagnóstico)
    url_template: str = "{base}/{cep}/json/"   # como montar a URL de um CEP
    http_404_nao_existe: bool = False          # fontes estáticas (OpenCEP) respondem 404 p/ CEP inexistente


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
        url = self.cfg.url_template.format(base=self.cfg.base_url, cep=cep)
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
                elif code == 404 and self.cfg.http_404_nao_existe:
                    self._on_success()
                    return LookupResult(cep, NAO_EXISTE, detalhe=f"CEP não consta na base do {self.cfg.nome}")
                elif code == 400:
                    self._on_success()
                    return LookupResult(cep, CEP_INVALIDO, detalhe="HTTP 400 (formato rejeitado pela API)")
                elif code == 429:
                    self._handle_throttle(resp)  # pausa, ou levanta BlockedError
                    continue
                elif code == 403:
                    raise BlockedError(
                        f"HTTP 403 do {self.cfg.nome}: acesso possivelmente bloqueado. "
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
    def _parse_200(self, cep: str, resp: Any) -> Optional[LookupResult]:
        try:
            data = resp.json()
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        erro = data.get("erro")
        if erro is True or (isinstance(erro, str) and erro.strip().lower() == "true"):
            return LookupResult(cep, NAO_EXISTE, detalhe=f"CEP não consta na base do {self.cfg.nome}")
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
                f"Rede ou {self.cfg.nome} indisponível; progresso já salvo."
            )
        log.warning("CEP %s falhou: %s - %s", cep, status, detail)
        return LookupResult(cep, status, detalhe=detail)

    def _handle_throttle(self, resp: Any) -> None:
        self._throttle_events += 1
        if self._throttle_events >= self.cfg.max_throttle_events:
            raise BlockedError(
                f"HTTP 429 recebido {self._throttle_events} vezes seguidas: limite ou bloqueio "
                f"do {self.cfg.nome}. Interrompido; aguarde (possivelmente horas) antes de retomar."
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


# --------------------------------------------------------------------------- fontes
def viacep_config(**kw: Any) -> ClientConfig:
    return ClientConfig(**kw)


def opencep_config(**kw: Any) -> ClientConfig:
    """OpenCEP: resposta no mesmo formato do ViaCEP (localidade/uf); 404 = CEP inexistente."""
    kw.setdefault("base_url", "https://opencep.com/v1")
    kw.setdefault("url_template", "{base}/{cep}")
    kw.setdefault("nome", "OpenCEP")
    kw.setdefault("http_404_nao_existe", True)
    return ClientConfig(**kw)


class FallbackClient:
    """ViaCEP primeiro; se a rede/ViaCEP falhar, usa a fonte reserva (OpenCEP).

    * Resultado da reserva vem marcado no diagnóstico ("fonte: OpenCEP").
    * "Não existe" na reserva NÃO é definitivo (a base dela pode ser diferente da do ViaCEP):
      vira ERRO_REDE para ser reconsultado no ViaCEP numa próxima execução.
    * Depois de uma falha séria do ViaCEP (bloqueio, ou vários CEPs seguidos sem resposta), o
      ViaCEP descansa ``cooldown`` segundos e só então é testado de novo (não é martelado).
    """

    def __init__(self, primary: Any, secondary: Any, cooldown: float = 900.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.primary, self.secondary = primary, secondary
        self.cooldown = cooldown
        self._clock = clock
        self._primary_off_until: Optional[float] = None
        self.fallback_used = 0

    @property
    def requests_made(self) -> int:
        return getattr(self.primary, "requests_made", 0) + getattr(self.secondary, "requests_made", 0)

    def _primary_available(self) -> bool:
        return self._primary_off_until is None or self._clock() >= self._primary_off_until

    def lookup(self, cep: str) -> LookupResult:
        if self._primary_available():
            try:
                res = self.primary.lookup(cep)
            except (BlockedError, ServiceUnavailableError) as exc:
                self._primary_off_until = self._clock() + self.cooldown
                log.warning("%s fora do ar (%s); usando a fonte reserva por %.0f s", self.primary.cfg.nome, exc, self.cooldown)
            else:
                if res.status not in (ERRO_REDE, ERRO_API):
                    self._primary_off_until = None
                    return res
        return self._via_secondary(cep)

    def _via_secondary(self, cep: str) -> LookupResult:
        res = self.secondary.lookup(cep)  # se a reserva também abortar, a exceção segue para o processor
        self.fallback_used += 1
        nome = self.secondary.cfg.nome
        if res.status == OK:
            return LookupResult(cep, OK, res.cidade, res.uf, detalhe=f"fonte: {nome} ({self.primary.cfg.nome} indisponível)")
        if res.status == NAO_EXISTE:
            return LookupResult(cep, ERRO_REDE, detalhe=f"{self.primary.cfg.nome} indisponível e {nome} não conhece o CEP (confirmar no {self.primary.cfg.nome} depois)")
        return res


FONTES = ("viacep-reserva", "viacep", "opencep")


def build_client(fonte: str = "viacep-reserva", min_interval: float = 1.0) -> Any:
    """viacep-reserva (padrão): ViaCEP com OpenCEP de reserva | viacep | opencep."""
    if fonte == "viacep":
        return ViaCepClient(viacep_config(min_interval=min_interval))
    if fonte == "opencep":
        return ViaCepClient(opencep_config(min_interval=min_interval))
    if fonte == "viacep-reserva":
        return FallbackClient(
            ViaCepClient(viacep_config(min_interval=min_interval, max_retries=1)),
            ViaCepClient(opencep_config(min_interval=min_interval)),
        )
    raise ValueError(f"fonte desconhecida: {fonte}")
