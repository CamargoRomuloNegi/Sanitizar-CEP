import pytest
import requests

from cep_sanitizer.client import BlockedError, ServiceUnavailableError
from cep_sanitizer.models import CEP_INVALIDO, ERRO_API, ERRO_REDE, NAO_EXISTE, OK

from conftest import FakeResponse, ok_response


def test_cep_encontrado(make_client):
    client, session, _ = make_client(lambda cep, n: ok_response("Rio Claro", "sp"))
    r = client.lookup("13514002")
    assert (r.status, r.cidade, r.uf) == (OK, "Rio Claro", "SP")  # UF normalizada em maiúsculas
    assert session.calls == ["13514002"]


@pytest.mark.parametrize("erro", [True, "true", "True"])
def test_erro_true_vira_nao_existe(make_client, erro):
    client, _, _ = make_client(lambda cep, n: FakeResponse(200, {"erro": erro}))
    assert client.lookup("99999999").status == NAO_EXISTE


def test_http_400_vira_cep_invalido(make_client):
    client, session, _ = make_client(lambda cep, n: FakeResponse(400))
    assert client.lookup("12345678").status == CEP_INVALIDO
    assert len(session.calls) == 1  # sem retry


def test_5xx_tenta_de_novo_com_espera_crescente(make_client):
    handler = lambda cep, n: FakeResponse(503) if n < 3 else ok_response()
    client, session, ft = make_client(handler)
    assert client.lookup("01001000").status == OK
    assert len(session.calls) == 3
    assert ft.sleeps == [2.0, 4.0]


def test_timeouts_esgotam_tentativas(make_client):
    def handler(cep, n):
        raise requests.Timeout()

    client, session, ft = make_client(handler)
    r = client.lookup("01001000")
    assert r.status == ERRO_REDE
    assert len(session.calls) == 4  # 1 + 3 novas tentativas
    assert ft.sleeps == [2.0, 4.0, 8.0]


def test_erro_de_conexao_vira_erro_rede(make_client):
    def handler(cep, n):
        raise requests.ConnectionError()

    client, _, _ = make_client(handler, max_retries=0)
    assert client.lookup("01001000").status == ERRO_REDE


def test_429_respeita_retry_after_e_continua(make_client):
    handler = lambda cep, n: FakeResponse(429, headers={"Retry-After": "7"}) if n == 1 else ok_response()
    client, session, ft = make_client(handler)
    assert client.lookup("01001000").status == OK
    assert 7.0 in ft.sleeps
    assert len(session.calls) == 2


def test_429_persistente_aborta_com_blocked(make_client):
    client, session, ft = make_client(lambda cep, n: FakeResponse(429))
    with pytest.raises(BlockedError):
        client.lookup("01001000")
    assert len(session.calls) == 3
    assert ft.sleeps == [60.0, 120.0]  # pausas crescentes antes de desistir


def test_429_isolado_nao_acumula_apos_sucesso(make_client):
    # 429, ok, 429, ok, 429, ok: nunca 3 seguidos, então nunca aborta
    handler = lambda cep, n: FakeResponse(429, headers={"Retry-After": "1"}) if n == 1 else ok_response()
    client, _, _ = make_client(handler)
    for cep in ("01001000", "01310100", "20040020"):
        assert client.lookup(cep).status == OK


def test_403_aborta_na_hora(make_client):
    client, session, _ = make_client(lambda cep, n: FakeResponse(403))
    with pytest.raises(BlockedError):
        client.lookup("01001000")
    assert len(session.calls) == 1


def test_corpo_nao_json_e_tentado_de_novo_e_vira_erro_api(make_client):
    client, session, _ = make_client(lambda cep, n: FakeResponse(200, json_error=True))
    r = client.lookup("01001000")
    assert r.status == ERRO_API
    assert len(session.calls) == 4


def test_json_sem_localidade_nao_tenta_de_novo(make_client):
    client, session, _ = make_client(lambda cep, n: FakeResponse(200, {"cep": "01001-000"}))
    r = client.lookup("01001000")
    assert r.status == ERRO_API
    assert len(session.calls) == 1


def test_404_nao_tenta_de_novo(make_client):
    client, session, _ = make_client(lambda cep, n: FakeResponse(404))
    assert client.lookup("01001000").status == ERRO_API
    assert len(session.calls) == 1


def test_circuit_breaker_apos_ceps_seguidos_com_falha(make_client):
    def handler(cep, n):
        raise requests.Timeout()

    client, _, _ = make_client(handler, max_retries=0, max_failed_ceps_in_row=2)
    assert client.lookup("01001000").status == ERRO_REDE
    with pytest.raises(ServiceUnavailableError):
        client.lookup("01310100")


def test_sucesso_zera_contador_de_falhas(make_client):
    def handler(cep, n):
        if cep.startswith("9"):
            raise requests.Timeout()
        return ok_response()

    client, _, _ = make_client(handler, max_retries=0, max_failed_ceps_in_row=2)
    for cep in ("90000001", "01001000", "90000002", "01310100", "90000003"):
        client.lookup(cep)  # nunca 2 falhas seguidas: não deve levantar


def test_intervalo_minimo_entre_consultas(make_client):
    starts = []
    clock_ref = {}

    def handler(cep, n):
        starts.append(clock_ref["ft"].now)
        return ok_response()

    client, _, ft = make_client(handler)
    clock_ref["ft"] = ft
    for cep in ("01001000", "01310100", "20040020", "30130010"):
        client.lookup(cep)
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert all(g >= 1.0 for g in gaps)


def test_jitter_so_acrescenta_tempo(make_client):
    starts = []
    clock_ref = {}

    def handler(cep, n):
        starts.append(clock_ref["ft"].now)
        return ok_response()

    client, _, ft = make_client(handler, rng=lambda: 1.0, jitter=0.5)
    clock_ref["ft"] = ft
    client.lookup("01001000")
    client.lookup("01310100")
    assert starts[1] - starts[0] == pytest.approx(1.5)


def test_cabecalhos_nao_vazam_dados_pessoais(make_client):
    client, session, _ = make_client(lambda cep, n: ok_response())
    client.lookup("01001000")
    assert "User-Agent" in session.last_headers
    assert "@" not in " ".join(session.last_headers.values())


# ---------------------------------------------------------------- fonte reserva (OpenCEP)
def _fallback(viacep_handler, opencep_handler, cooldown=900.0):
    from cep_sanitizer.client import FallbackClient, ViaCepClient, opencep_config, viacep_config
    from conftest import FakeSession, FakeTime

    ft = FakeTime()
    sp, so = FakeSession(viacep_handler), FakeSession(opencep_handler)

    class _S(FakeSession):  # OpenCEP: .../v1/<cep> (sem /json/)
        def get(self, url, timeout=None, headers=None):
            cep = url.rstrip("/").split("/")[-1]
            self.calls.append(cep)
            return self.handler(cep, self.calls.count(cep))

    so = _S(opencep_handler)
    kw = dict(min_interval=0.0, jitter=0.0)
    prim = ViaCepClient(viacep_config(max_retries=0, max_failed_ceps_in_row=2, **kw), session=sp,
                        sleep=ft.sleep, clock=ft.clock)
    sec = ViaCepClient(opencep_config(max_retries=0, **kw), session=so, sleep=ft.sleep, clock=ft.clock)
    return FallbackClient(prim, sec, cooldown=cooldown, clock=ft.clock), sp, so, ft


def _boom(cep, n):
    import requests
    raise requests.ConnectionError("reset")


def test_viacep_ok_nao_toca_na_reserva():
    from conftest import ok_response
    fb, sp, so, _ = _fallback(lambda c, n: ok_response(), _boom)
    assert fb.lookup("01001000").status == "OK" and so.calls == []


def test_viacep_cai_e_reserva_resolve_com_marca_de_fonte():
    from conftest import ok_response
    fb, sp, so, _ = _fallback(_boom, lambda c, n: ok_response("Campinas", "SP"))
    r = fb.lookup("13010000")
    assert (r.status, r.cidade, r.uf) == ("OK", "Campinas", "SP") and "fonte: OpenCEP" in r.detalhe


def test_inexistente_na_reserva_nao_e_definitivo():
    from conftest import FakeResponse
    fb, *_ = _fallback(_boom, lambda c, n: FakeResponse(404))
    r = fb.lookup("99999999")
    assert r.status == "ERRO_REDE" and r.status not in __import__("cep_sanitizer.models", fromlist=["x"]).DEFINITIVOS


def test_viacep_descansa_no_cooldown_e_volta_depois():
    from conftest import ok_response
    fb, sp, so, ft = _fallback(_boom, lambda c, n: ok_response(), cooldown=900)
    for cep in ("01001000", "01001001", "01001002"):  # 2 falhas seguidas abrem o disjuntor
        fb.lookup(cep)
    antes = len(sp.calls)
    fb.lookup("01001003")
    assert len(sp.calls) == antes  # em cooldown: ViaCEP não é consultado
    ft.now += 901
    fb.lookup("01001004")
    assert len(sp.calls) == antes + 1  # cooldown passou: testa o ViaCEP de novo


def test_nao_existe_no_viacep_e_definitivo_sem_reserva():
    from conftest import FakeResponse
    fb, sp, so, _ = _fallback(lambda c, n: FakeResponse(200, {"erro": "true"}), _boom)
    assert fb.lookup("99999999").status == "NAO_EXISTE" and so.calls == []


def test_opencep_404_vira_nao_existe_quando_e_a_fonte_unica():
    from conftest import FakeResponse
    from cep_sanitizer.client import build_client
    c = build_client("opencep", 0.0)
    assert c.cfg.nome == "OpenCEP" and c.cfg.http_404_nao_existe
    assert c.cfg.url_template.format(base=c.cfg.base_url, cep="01001000").endswith("/v1/01001000")
