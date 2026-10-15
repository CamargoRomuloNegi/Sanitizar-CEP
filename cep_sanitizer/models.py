"""Constantes e tipos compartilhados."""

from __future__ import annotations

from dataclasses import dataclass

# Texto gravado nas colunas de resultado quando não foi possível obter cidade/UF.
NAO_ENCONTRADO = "não encontrado"

# Códigos de diagnóstico (aparecem como prefixo da coluna "Diagnóstico CEP").
OK = "OK"                      # CEP encontrado
NAO_EXISTE = "NAO_EXISTE"      # formato válido, mas o ViaCEP não conhece o CEP
CEP_INVALIDO = "CEP_INVALIDO"  # formato inválido (nem foi enviado à API)
CEP_VAZIO = "CEP_VAZIO"        # célula em branco
ERRO_REDE = "ERRO_REDE"        # timeout / conexão (transitório; tentará de novo na próxima execução)
ERRO_API = "ERRO_API"          # resposta inesperada da API (transitório)
PENDENTE = "PENDENTE"          # ainda não consultado (limite da execução ou interrupção)
FORA_DO_FILTRO = "FORA_DO_FILTRO"  # linha fora do filtro "Em uso": não consultada de propósito

# Valores da coluna "Status" (veredito por linha, comparando o ViaCEP com a Cidade/UF da base).
STATUS_NAO_ENCONTRADO = "Não Encontrado"      # CEP em branco/inválido/inexistente ou consulta com erro
STATUS_OK = "OK Cidade e UF"                  # Cidade e UF da base conferem com o ViaCEP
STATUS_REVER = "Rever - CIDADE ou UF"         # CEP existe, mas cidade e/ou UF da base divergem

# Somente resultados definitivos vão para o cache em disco.
# Erros transitórios NÃO são gravados, para serem tentados de novo numa próxima execução.
DEFINITIVOS = frozenset({OK, NAO_EXISTE})


@dataclass(frozen=True)
class LookupResult:
    """Resultado da consulta de um CEP (8 dígitos)."""

    cep: str
    status: str
    cidade: str = ""
    uf: str = ""
    detalhe: str = ""
