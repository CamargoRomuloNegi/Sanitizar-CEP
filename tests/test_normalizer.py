import pytest

from cep_sanitizer.models import CEP_INVALIDO, CEP_VAZIO, OK
from cep_sanitizer.normalizer import normalize_cep


@pytest.mark.parametrize(
    "valor, esperado",
    [
        ("01001000", "01001000"),
        ("01001-000", "01001000"),
        ("01.001-000", "01001000"),
        ("  01001000  ", "01001000"),
        ("01001000   ", "01001000"),   # campo de largura fixa, como na base real
        ("13514002", "13514002"),
        ("13.514-002", "13514002"),
        # decimal do Excel em CEP de 8 dígitos
        ("13514002.0", "13514002"),
        (13514002.0, "13514002"),
        (13514002, "13514002"),
    ],
)
def test_formatos_aceitos(valor, esperado):
    n = normalize_cep(valor)
    assert n.status == OK
    assert n.cep == esperado
    assert n.padded is False


@pytest.mark.parametrize(
    "valor, completado_esperado",
    [
        ("1001000", "01001000"),
        ("1.001-000", "01001000"),
        ("1001000.0", "01001000"),
        ("1001000,0", "01001000"),
        (1001000, "01001000"),
        (1001000.0, "01001000"),
        ("4500000 ", "04500000"),   # Vitória da Conquista/BA na base real: aqui o zero seria ERRADO
    ],
)
def test_sete_digitos_so_completa_zero_se_pedido(valor, completado_esperado):
    # Padrão: inválido (provavelmente truncado), como na base real.
    padrao = normalize_cep(valor)
    assert padrao.status == CEP_INVALIDO and padrao.cep is None and "truncado" in padrao.detalhe
    # Opt-in (planilha que passou pelo Excel): completa um zero à esquerda.
    completado = normalize_cep(valor, pad_leading_zero=True)
    assert completado.status == OK and completado.padded is True
    assert completado.cep == completado_esperado


@pytest.mark.parametrize(
    "valor",
    [
        "123456",          # 6 dígitos: não adivinhamos
        "12345",
        "950100100",       # 9 dígitos (exemplo da doc do ViaCEP)
        "95010A10",        # alfanumérico (exemplo da doc)
        "95010 10",        # espaço no meio (exemplo da doc)
        "ABCDEFGH",
        "00000000",
        "0000000",
        "N/A",
        "-",
        "sem cep",
        "12345-67",
        "12-345-678",
        "1.2.3",
        "01001.000",
        "1234567890123",
        "١٢٣٤٥٦٧٨",        # dígitos não-ASCII
    ],
)
def test_formatos_invalidos(valor):
    n = normalize_cep(valor)
    assert n.status == CEP_INVALIDO
    assert n.cep is None


@pytest.mark.parametrize("valor", ["", "   ", None, float("nan"), "nan", "NaN", "NULL", "None"])
def test_vazios(valor):
    n = normalize_cep(valor)
    assert n.status == CEP_VAZIO
    assert n.cep is None
