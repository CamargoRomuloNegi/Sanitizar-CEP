import io

import openpyxl
import pandas as pd
import pytest

from cep_sanitizer.io_utils import (
    TableMeta,
    detect_separator,
    list_sheets,
    read_table,
    write_table,
    write_table_to_path,
)


def test_csv_ponto_e_virgula_preserva_zeros(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("Nome;CEP\nAna;01001000\nBia;\n", encoding="utf-8")
    df, meta = read_table(p)
    assert meta.sep == ";" and df["CEP"].tolist() == ["01001000", ""]  # vazio continua string, não NaN


def test_csv_virgula_detectada(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("Nome,CEP\nAna,01001000\n", encoding="utf-8")
    df, meta = read_table(p)
    assert meta.sep == "," and df.columns.tolist() == ["Nome", "CEP"]


def test_csv_cp1252_com_acentos(tmp_path):
    p = tmp_path / "a.csv"
    p.write_bytes("Nome;CEP\nJosé;01001000\n".encode("cp1252"))
    df, meta = read_table(p)
    assert meta.encoding == "cp1252" and df.loc[0, "Nome"] == "José"


def test_csv_utf8_com_bom(tmp_path):
    p = tmp_path / "a.csv"
    p.write_bytes("Nome;CEP\nJosé;01001000\n".encode("utf-8-sig"))
    df, meta = read_table(p)
    assert df.columns.tolist() == ["Nome", "CEP"] and df.loc[0, "Nome"] == "José"


def test_leitura_a_partir_de_bytes():
    df, meta = read_table(b"CEP;X\n01001000;1\n", filename="upload.csv")
    assert df.loc[0, "CEP"] == "01001000" and meta.kind == "csv"


def test_formato_nao_suportado():
    with pytest.raises(ValueError):
        read_table(b"x", filename="a.xls")


def test_escrita_csv_mantem_separador_e_codificacao():
    df = pd.DataFrame({"Cidade": ["São Paulo"], "CEP": ["01001000"]})
    meta = TableMeta(kind="csv", sep=";", encoding="cp1252")
    raw = write_table(df, meta)
    assert raw.decode("cp1252").splitlines() == ["Cidade;CEP", "São Paulo;01001000"]


def test_escrita_csv_utf8_tem_bom_para_o_excel():
    raw = write_table(pd.DataFrame({"a": ["ã"]}), TableMeta(kind="csv", sep=";", encoding="utf-8-sig"))
    assert raw.startswith(b"\xef\xbb\xbf")


def test_escrita_em_disco_roundtrip(tmp_path):
    df = pd.DataFrame({"CEP": ["01001000"], "Cidade": ["São Paulo"]})
    meta = TableMeta(kind="csv", sep=";", encoding="utf-8-sig")
    destino = tmp_path / "o.csv"
    write_table_to_path(df, meta, destino)
    lido, _ = read_table(destino)
    pd.testing.assert_frame_equal(lido, df)


def _xlsx_bytes():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Base"
    ws.append(["CEP", "Situação"])
    ws.append(["01001000", "Em uso"])   # texto com zero à esquerda
    ws.append([1001000, "Em uso"])      # número inteiro (zero perdido)
    ws.append([13514002.0, "Em uso"])   # número decimal
    wb.create_sheet("Outra").append(["x"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_xlsx_le_como_texto_e_lista_abas():
    data = _xlsx_bytes()
    assert list_sheets(data) == ["Base", "Outra"]
    df, meta = read_table(data, filename="a.xlsx")
    assert meta.kind == "xlsx"
    assert df["CEP"].tolist()[0] == "01001000"
    assert df["CEP"].tolist()[1] == "1001000"


def test_xlsx_escolhe_aba():
    df, meta = read_table(_xlsx_bytes(), filename="a.xlsx", sheet="Outra")
    assert df.columns.tolist() == ["x"] and meta.sheet == "Outra"


def test_xlsx_roundtrip_mantem_cep_como_texto():
    df = pd.DataFrame({"CEP": ["01001000"], "Cidade Retornada": ["São Paulo"]})
    raw = write_table(df, TableMeta(kind="xlsx"))
    lido, _ = read_table(raw, filename="o.xlsx")
    pd.testing.assert_frame_equal(lido, df)


def test_detect_separator_padrao():
    assert detect_separator(b"") == ";"
    assert detect_separator(b"CEP\n01001000\n") == ";"
    assert detect_separator(b"a\tb\tc\n") == "\t"
