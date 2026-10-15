"""Dublês de teste: relógio falso, sessão HTTP falsa e cliente falso. Nenhum teste usa rede."""

from __future__ import annotations

import pytest

from cep_sanitizer.client import ClientConfig, ViaCepClient
from cep_sanitizer.models import NAO_EXISTE, OK, LookupResult


class FakeTime:
    """Relógio controlável: ``sleep`` avança o tempo sem esperar de verdade."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None, json_error=False):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("corpo não é JSON")
        return self._payload


def ok_response(cidade="São Paulo", uf="SP") -> FakeResponse:
    return FakeResponse(200, {"cep": "x", "localidade": cidade, "uf": uf})


class FakeSession:
    """``handler(cep, n_chamada_deste_cep)`` devolve FakeResponse ou levanta exceção."""

    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls: list[str] = []
        self.last_headers: dict = {}

    def get(self, url, timeout=None, headers=None):
        cep = url.rstrip("/").split("/")[-2]
        self.calls.append(cep)
        self.last_headers = headers or {}
        return self.handler(cep, self.calls.count(cep))


@pytest.fixture
def make_client():
    def _make(handler, rng=lambda: 0.0, **cfg):
        cfg.setdefault("min_interval", 1.0)
        cfg.setdefault("jitter", 0.0)
        fake_time = FakeTime()
        session = FakeSession(handler)
        client = ViaCepClient(
            ClientConfig(**cfg), session=session,
            sleep=fake_time.sleep, clock=fake_time.clock, rng=rng,
        )
        return client, session, fake_time

    return _make


class FakeClient:
    """Cliente falso para testar o processor sem passar pelo HTTP."""

    def __init__(self, table=None, default=None, fail_with=None, fail_at=None):
        self.table = table or {}
        self.default = default
        self.fail_with = fail_with
        self.fail_at = fail_at
        self.calls: list[str] = []

    def lookup(self, cep: str) -> LookupResult:
        if self.fail_at is not None and len(self.calls) >= self.fail_at:
            raise self.fail_with
        self.calls.append(cep)
        if cep in self.table:
            value = self.table[cep]
            if isinstance(value, LookupResult):
                return value
            cidade, uf = value
            return LookupResult(cep, OK, cidade, uf)
        if self.default is not None:
            return self.default(cep)
        return LookupResult(cep, NAO_EXISTE, detalhe="CEP não consta na base do ViaCEP")


@pytest.fixture
def fake_client_cls():
    return FakeClient
