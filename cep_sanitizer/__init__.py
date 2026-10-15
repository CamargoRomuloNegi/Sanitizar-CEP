"""cep-sanitizer: sanitização de CEPs via ViaCEP com consultas controladas.

Módulos:
    models      constantes de status e o tipo ``LookupResult``
    normalizer  limpeza/validação do CEP vindo da planilha (sem rede)
    client      cliente ViaCEP com limitador de taxa, retry e detecção de bloqueio
    cache       cache em disco (JSONL) que permite retomar uma execução interrompida
    processor   leitura do plano de trabalho e preenchimento do DataFrame
    io_utils    leitura/escrita de CSV e XLSX
    cli         execução por linha de comando (recomendada para a base completa)
"""

__version__ = "1.0.0"
