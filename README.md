# Sanitizador de CEP

Lê uma planilha (`.csv` ou `.xlsx`), consulta cada **CEP único** no [ViaCEP](https://viacep.com.br) e devolve o arquivo **completo** (todas as linhas da entrada, com as colunas originais intactas) mais quatro colunas:

| Coluna nova | Conteúdo |
|---|---|
| `Cidade Retornada` | cidade devolvida pelo ViaCEP (ou `não encontrado`) |
| `Estado Retornado` | sigla da UF devolvida pelo ViaCEP (ou `não encontrado`) |
| `Status` | veredito da linha, comparando o ViaCEP com a **Cidade e a UF da própria base** (tabela abaixo) |
| `Diagnóstico CEP` | código técnico do que aconteceu, com o motivo (tabela mais abaixo) |

## Coluna `Status`

| Status | Quando |
|---|---|
| `Não Encontrado` | CEP em branco, com menos de 8 dígitos (inclui 5 e 7), inexistente no ViaCEP, ou consulta com erro |
| `OK Cidade e UF` | o CEP existe e a Cidade **e** a UF da base conferem com o ViaCEP |
| `Rever - CIDADE ou UF` | o CEP existe, mas a cidade e/ou a UF da base divergem (o `Diagnóstico CEP` diz qual e quais são os dois valores) |
| *(em branco)* | linha fora do filtro (ex.: `Usado = 0`, não consultada) ou ainda pendente |

A comparação ignora acento, caixa, pontuação e espaços de preenchimento: `Sao Paulo   `, `SÃO PAULO` e `São Paulo` são iguais; `Ji-Parana` = `Ji-Paraná`. Abreviações (`Sta Barbara`) e nomes antigos de município aparecem como `Rever`, que é o objetivo. As colunas da base são detectadas pelo nome (`Cidade`/`Municipio`/`Localidade` e `UF`/`Estado`) ou informadas em `--coluna-cidade` e `--coluna-uf`. Sem elas (ou com `--sem-comparacao`) a coluna `Status` não é criada.

---

## Leia antes de rodar

O ViaCEP avisa em seu site: *"uso massivo para validação de bases de dados locais poderá automaticamente bloquear seu acesso por tempo indeterminado"*. O projeto reduz o risco, mas não o elimina:

- consulta só linhas dentro do filtro (ex.: `Usado > 0`) e só **CEPs únicos**;
- uma consulta por vez, com intervalo mínimo (padrão **1 s** + jitter) e piso de segurança de 0,25 s na CLI / 0,5 s no app;
- **para sozinho** em HTTP 403, em 3 respostas 429 seguidas ou em 5 CEPs seguidos com falha, e grava o progresso;
- retoma de onde parou (cache em disco), então dá para fatiar a execução em vários dias.

**Tempo realista** (≈ 1,4 s por CEP único no ritmo padrão: intervalo + jitter + latência):

| CEPs únicos a consultar | Tempo aproximado |
|---|---|
| 10.000 | ~4 h |
| 15.000 | ~5h50 |
| 50.000 | ~19 h |
| 300.000 | ~4,9 dias |

Rode `--dry-run` primeiro para ver o número real de CEPs únicos. Se for muito alto, veja [Alternativas](#alternativas-para-bases-grandes).

---

## Estrutura

```
app.py                      interface Streamlit (lotes menores e conferência)
cep_sanitizer/
  cli.py                    execução por linha de comando (recomendada para a base completa)
  client.py                 cliente ViaCEP: limitador de taxa, retry, detecção de bloqueio
  cache.py                  cache JSONL em disco (retomada)
  normalizer.py             limpeza e validação do CEP
  processor.py              filtro, deduplicação, preenchimento das colunas e Status
  io_utils.py               leitura/escrita de CSV e XLSX (com ou sem cabeçalho)
tools/
  gerar_massa_teste.py      gera a massa de 1.000 CEPs (ou amostra da sua base) + gabarito
  validar_resultado.py      confere a saída contra o gabarito
dados_exemplo/              massa_teste_1000.csv (sem cabeçalho) e massa_teste_1000_gabarito.csv
tests/                      167 testes automatizados (sem rede)
```

## Instalação

```bash
git clone <seu-repositorio> && cd <seu-repositorio>
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
```

## Uso pela linha de comando (base completa)

Exemplo com a base `NP_CEP.csv` (`CEP;Cidade;UF;Bairro;Usado`, **sem cabeçalho**), consultando só `Usado > 0`:

```bash
# 1) Estimar sem consultar nada
python -m cep_sanitizer NP_CEP.csv --sem-cabecalho --filtro-coluna Usado --filtro-maior-que 0 --dry-run

# 2) Rodar. Ctrl+C a qualquer momento grava a saída parcial; rodar o MESMO comando continua de onde parou
python -m cep_sanitizer NP_CEP.csv --sem-cabecalho --filtro-coluna Usado --filtro-maior-que 0

# 3) Ou fatiar: no máximo 5.000 consultas novas por execução
python -m cep_sanitizer NP_CEP.csv --sem-cabecalho --filtro-coluna Usado --filtro-maior-que 0 --max-consultas 5000
```

- A saída vai para `<entrada>_sanitizado.csv` (**todas as linhas**) e o progresso para `<entrada>.csv.cache_cep.jsonl`. **Guarde o cache**: é ele que evita repetir consultas.
- Linhas ainda não consultadas saem com `Diagnóstico CEP = PENDENTE` e `Status` em branco; ao rodar de novo elas são preenchidas.
- Código de saída: `0` concluído; `1` erro de uso/arquivo; `2` incompleto (limite, interrupção, bloqueio ou indisponibilidade).
- Se aparecer "possível bloqueio", **espere várias horas** antes de retomar e considere aumentar `--intervalo`.
- Linhas com `ERRO_REDE`/`ERRO_API` (falha transitória) **não vão para o cache**: rode de novo até não restar nenhuma.

Outras opções: `-o saida.csv`, `--coluna-cep NOME`, `--intervalo 1.5`, `--cache arquivo.jsonl`, `--aba NOME` (XLSX), `--sep ";"`, `--encoding cp1252`.

### Filtro: o que NÃO deve ser consultado

Com `--filtro-coluna COLUNA`, use **exatamente uma** destas regras:

| Regra | Consulta... | Exemplo |
|---|---|---|
| `--filtro-maior-que N` | só linhas cujo valor numérico é maior que N. Vazio ou texto fica fora (e é contado e avisado) | `--filtro-coluna Usado --filtro-maior-que 0` |
| `--filtro-valor V` | só linhas com o valor V (repetível; ignora caixa, acento e espaços) | `--filtro-coluna Situação --filtro-valor "Em uso"` |
| `--filtro-excluir V` | tudo, exceto linhas com o valor V (repetível) | `--filtro-coluna Usado --filtro-excluir 0` |

Linhas fora do filtro **nunca** são consultadas, mesmo que o CEP delas apareça também em linhas dentro do filtro. Ficam com cidade, estado e `Status` em branco e `Diagnóstico CEP = FORA_DO_FILTRO`.

### Arquivo sem cabeçalho

Exportações de banco costumam não ter linha de cabeçalho. Sem `--sem-cabecalho`, a primeira linha é tratada como nome de coluna e **o primeiro registro se perde** (o programa avisa quando os "nomes" parecem dados). Com a opção, os nomes vêm de `--colunas` (padrão `CEP,Cidade,UF,Bairro,Usado`). A saída inclui uma linha de cabeçalho com esses nomes mais as colunas novas.

### CEP com menos de 8 dígitos

Por padrão, CEP em branco, de 5 dígitos ou de 7 dígitos vira `não encontrado` (`Status = Não Encontrado`) e **não é enviado à API**. Em bases exportadas de sistema, o CEP de 7 dígitos costuma estar *truncado* (perdeu o último dígito), e completar com zero consultaria o CEP errado: na base `NP_CEP.csv`, `4500000` (Vitória da Conquista/BA) viraria `04500000`, que é de SP. Se a sua planilha passou pelo **Excel**, que apaga o zero inicial, use `--completar-zero`: o CEP de 7 dígitos recebe um zero à esquerda, e o diagnóstico registra o CEP efetivamente consultado.

## Uso pelo Streamlit

```bash
streamlit run app.py
```

1. Envie a planilha; marque "A planilha NÃO tem linha de cabeçalho" se for o caso e ajuste os nomes das colunas.
2. Escolha a coluna do CEP, as colunas de Cidade e UF da base (para o `Status`) e a regra do filtro.
3. Veja a estimativa (CEPs únicos, já no cache, a consultar).
4. Clique em **Iniciar consultas**. Cada clique faz no máximo "Máx. de consultas novas por execução" (padrão 500) e continua do cache no clique seguinte.
5. Baixe a planilha preenchida e, ao terminar, **baixe também o cache**: no Streamlit Cloud o disco não persiste entre sessões, e você pode reenviar o cache na próxima vez.

Não mexa nos controles da página enquanto roda (isso interrompe a execução; o que já foi consultado fica no cache).

## Publicar no GitHub e no Streamlit Cloud

```bash
git init && git add . && git commit -m "Sanitizador de CEP"
git branch -M main
git remote add origin https://github.com/SEU-USUARIO/cep-sanitizer.git
git push -u origin main
```

1. Acesse [share.streamlit.io](https://share.streamlit.io) e entre com o GitHub.
2. **Create app** → escolha o repositório, branch `main` e arquivo principal `app.py`.
3. Em **Advanced settings**, selecione Python 3.10 ou superior. Dependências vêm de `requirements.txt`.
4. **Deploy.**

O workflow `.github/workflows/tests.yml` roda os testes a cada push. O `.gitignore` já exclui caches e saídas de execuções reais, para você não subir dados da sua base por engano.

> O Streamlit Cloud serve bem para conferência e lotes pequenos, mas **não para rodadas de horas ou dias**: a sessão expira e o disco é descartado. Para a base completa use a CLI em uma máquina sua.

---

## Diagnósticos (`Diagnóstico CEP`)

| Código | Significado | Cidade/Estado retornados | Status |
|---|---|---|---|
| `OK` | encontrado. Se a base diverge, vem a nota `difere da base: cidade base 'X' x ViaCEP 'Y'` (e/ou UF) | preenchidos | `OK Cidade e UF` ou `Rever - CIDADE ou UF` |
| `NAO_EXISTE` | formato válido, mas o ViaCEP não conhece o CEP | `não encontrado` | `Não Encontrado` |
| `CEP_INVALIDO` | formato inválido, inclusive 5 e 7 dígitos (nem foi enviado à API) | `não encontrado` | `Não Encontrado` |
| `CEP_VAZIO` | célula em branco | `não encontrado` | `Não Encontrado` |
| `ERRO_REDE` / `ERRO_API` | timeout, conexão ou resposta inesperada. **Não vai para o cache**: a próxima execução tenta de novo | `não encontrado` | `Não Encontrado` |
| `FORA_DO_FILTRO` | linha fora do filtro (ex.: `Usado = 0`): não consultada de propósito | em branco | em branco |
| `PENDENTE` | ainda não consultada (limite da execução, interrupção ou bloqueio) | em branco | em branco |

`PENDENTE` e `FORA_DO_FILTRO` ficam em branco de propósito: gravar "não encontrado" ali seria dizer algo que o programa não verificou.

## Regras de normalização do CEP

- Aceita `01001000`, `01001-000` e `01.001-000`; ignora espaços nas pontas (a base usa campos de largura fixa, como `"11700   "`).
- Menos de 8 dígitos, letras, 9+ dígitos, espaço no meio, `00000000` e `N/A` são `CEP_INVALIDO`; célula em branco ou `NULL` é `CEP_VAZIO`.
- `13514002.0` (número decimal do Excel) é aceito como `13514002`.
- Com `--completar-zero`, 7 dígitos recebem um zero à esquerda (veja acima).
- A planilha é lida sempre como texto, então `01001000` nunca vira `1001000`.

## Fragilidades revistas

| Risco | Como foi tratado |
|---|---|
| Bloqueio do IP pelo ViaCEP | deduplicação, filtro por `Usado`, intervalo + jitter, parada em 403/429 persistente, retomada por cache |
| Queda de energia ou travamento no meio de horas de execução | cada resultado é gravado e descarregado no instante em que chega; linha truncada é ignorada ao reler |
| Erro de rede virar "não encontrado" definitivo | erros transitórios nunca entram no cache; a próxima execução os refaz |
| Ficar martelando um serviço fora do ar | aborta após 5 CEPs seguidos com falha |
| Consultar o CEP errado por "adivinhar" o zero | CEP de 7 dígitos não é completado por padrão |
| Primeiro registro engolido como cabeçalho | `--sem-cabecalho` e aviso automático quando os "nomes" parecem dados |
| Nome de cidade sem acento/caixa/padding gerando falso "Rever" | comparação normalizada |
| CSV com `;`, acentos em Windows-1252 ou BOM | separador e codificação detectados; saída preserva ambos |
| 600 mil linhas em memória | mapeamento por dicionário sobre CEPs únicos (teste de escala incluído) |
| Linha de uma execução incompleta confundida com resultado final | `PENDENTE` explícito e código de saída `2` |

**Riscos que continuam:** (1) o ViaCEP pode bloquear mesmo com consultas espaçadas, pois não publica um limite numérico; (2) a base do ViaCEP pode estar defasada em relação aos Correios; (3) `Rever` é um **alerta**, não uma sentença: pode ser erro da base, nome antigo de município, abreviação ou defasagem do ViaCEP, e exige olhar humano; (4) a CLI roda em uma máquina sua, que precisa ficar ligada.

---

## Testes

```bash
python -m pytest -q          # 167 testes, sem rede, ~4 s
```

Cobrem: formatos de CEP, 400/429/403/5xx/timeout/JSON inválido, `Retry-After`, intervalo mínimo e jitter, cache e linha truncada, filtros, deduplicação, limite por execução, bloqueio no meio da execução, retomada, CSV/XLSX com e sem cabeçalho, a comparação do `Status`, a CLI e uma execução com **600.000 linhas**.

### Massa de teste de 1.000 CEPs

`dados_exemplo/massa_teste_1000.csv` imita o layout da base real (**sem cabeçalho**, `CEP;Cidade;UF;Bairro;Usado`, cidade sem acento, CEP em campo de largura fixa) e o gabarito `massa_teste_1000_gabarito.csv`. Composição: 170 CEPs reais de capitais (em vários formatos; 40 deles com cidade ou UF divergentes, que devem sair `Rever`); 30 em branco; 30 de 5 dígitos; 30 de 7 dígitos; 90 inválidos; 60 de formato válido que não devem existir; 290 aleatórios plausíveis; 300 com `Usado = 0` (fora do escopo). São ~310 CEPs únicos, cerca de 7 minutos de consulta no ritmo padrão.

```bash
python -m cep_sanitizer dados_exemplo/massa_teste_1000.csv --sem-cabecalho \
    --filtro-coluna Usado --filtro-maior-que 0 -o saida_teste.csv
python tools/validar_resultado.py --saida saida_teste.csv \
    --gabarito dados_exemplo/massa_teste_1000_gabarito.csv
```

O validador classifica cada linha em `PASS`, `REVISAR` ou `FAIL`:

- `FAIL`: o app violou uma regra (CEP original alterado, `Status` incoerente com a comparação, inválido que virou OK, linha fora do filtro preenchida...).
- `REVISAR`: provável divergência de dado externo. As cidades das capitais foram informadas de memória (só `01001-000` vem da documentação do ViaCEP), então uma divergência ali pede conferência humana. Também entram aqui UF fora da faixa do CEP, erro transitório e linha pendente.

Para testar **com a sua base real**: `python tools/gerar_massa_teste.py --base NP_CEP.csv --sem-cabecalho --filtro-coluna Usado --filtro-maior-que 0` sorteia 1.000 linhas dela e gera o gabarito do que é determinístico.

---

## Alternativas para bases grandes

Se, depois do `--dry-run`, o número de CEPs únicos for alto demais para o ritmo seguro do ViaCEP:

1. **Falar com o ViaCEP** (há "Fale Conosco" no site) e perguntar sobre uso em lote.
2. **Base local de CEPs**: cruzar sua planilha com uma base de CEPs offline (por exemplo, o DNE dos Correios, que é licenciado, ou bases abertas, verificando a licença e a atualidade). Elimina a dependência de API.
3. **Outra API gratuita** (por exemplo, a BrasilAPI), conferindo limites e termos de uso antes. O `client.py` é isolado e pode ser trocado sem mexer no resto.
