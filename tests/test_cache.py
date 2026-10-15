from cep_sanitizer.cache import JournalCache
from cep_sanitizer.models import ERRO_API, ERRO_REDE, NAO_EXISTE, OK, LookupResult


def _ok(cep="01001000", cidade="São Paulo", uf="SP"):
    return LookupResult(cep, OK, cidade, uf)


def test_persiste_e_recarrega_com_acentos(tmp_path):
    path = tmp_path / "c.jsonl"
    with JournalCache(path) as cache:
        assert cache.put(_ok())
        assert cache.put(LookupResult("99999999", NAO_EXISTE))
    recarregado = JournalCache(path)
    assert len(recarregado) == 2
    assert recarregado.get("01001000").cidade == "São Paulo"
    assert recarregado.get("99999999").status == NAO_EXISTE
    assert "São Paulo" in path.read_text(encoding="utf-8")  # sem escapes ã


def test_erros_transitorios_nao_sao_gravados(tmp_path):
    path = tmp_path / "c.jsonl"
    with JournalCache(path) as cache:
        assert not cache.put(LookupResult("01001000", ERRO_REDE))
        assert not cache.put(LookupResult("01310100", ERRO_API))
    assert len(JournalCache(path)) == 0


def test_linha_truncada_e_ignorada_e_novo_registro_nao_se_perde(tmp_path):
    path = tmp_path / "c.jsonl"
    with JournalCache(path) as cache:
        cache.put(_ok("01001000"))
    with path.open("a", encoding="utf-8") as fh:  # simula queda no meio da gravação
        fh.write('{"cep": "01310100", "status": "O')
    cache = JournalCache(path)
    assert len(cache) == 1
    assert cache.skipped_lines == 1
    cache.put(_ok("20040020", "Rio de Janeiro", "RJ"))
    cache.close()
    final = JournalCache(path)
    assert set(final._data) == {"01001000", "20040020"}


def test_ignora_linhas_invalidas(tmp_path):
    path = tmp_path / "c.jsonl"
    path.write_text(
        '{"cep": "123", "status": "OK"}\n'
        '{"cep": "01001000", "status": "ERRO_REDE"}\n'
        'lixo\n'
        '{"cep": "01001000", "status": "OK", "cidade": "São Paulo", "uf": "SP"}\n',
        encoding="utf-8",
    )
    cache = JournalCache(path)
    assert len(cache) == 1
    assert cache.skipped_lines == 3


def test_merge_jsonl_persiste_em_disco(tmp_path):
    origem = JournalCache()
    origem.put(_ok())
    path = tmp_path / "destino.jsonl"
    with JournalCache(path) as destino:
        assert destino.merge_jsonl(origem.to_jsonl()) == 1
    assert JournalCache(path).get("01001000").uf == "SP"


def test_cache_em_memoria_nao_cria_arquivo(tmp_path):
    cache = JournalCache()
    cache.put(_ok())
    assert "01001000" in cache and len(cache) == 1
    assert list(tmp_path.iterdir()) == []
