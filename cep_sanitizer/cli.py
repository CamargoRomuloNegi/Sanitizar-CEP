"""Execução por linha de comando: o caminho recomendado para a base completa.

Exemplos:

    # 1) Só estimar (não consulta nada). Base sem cabeçalho, só linhas com Usado > 0:
    python -m cep_sanitizer NP_CEP.csv --sem-cabecalho --filtro-coluna Usado --filtro-maior-que 0 --dry-run

    # 2) Rodar (pode interromper com Ctrl+C e rodar o MESMO comando de novo para continuar):
    python -m cep_sanitizer NP_CEP.csv --sem-cabecalho --filtro-coluna Usado --filtro-maior-que 0

    # 3) Base com cabeçalho e coluna de situação ("Em uso"), em fatias de 20.000 consultas:
    python -m cep_sanitizer base.csv --filtro-coluna "Situação" --filtro-valor "Em uso" --max-consultas 20000

A saída traz TODAS as linhas da entrada, mais: Cidade Retornada, Estado Retornado, Status e
Diagnóstico CEP. O Status compara o ViaCEP com a Cidade e a UF da própria base (colunas
detectadas pelo nome ou informadas em --coluna-cidade / --coluna-uf).

Regras de filtro (use exatamente uma com --filtro-coluna):
    --filtro-valor V       consulta SÓ as linhas cujo valor é V (repetível)
    --filtro-excluir V     consulta tudo, EXCETO linhas cujo valor é V (repetível)
    --filtro-maior-que N   consulta SÓ as linhas cujo valor numérico é maior que N

Códigos de saída: 0 = tudo concluído; 1 = erro de uso/arquivo; 2 = incompleto (limite,
interrupção, bloqueio ou indisponibilidade). A planilha de saída é sempre gravada.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Optional

from .cache import JournalCache
from .client import ClientConfig, ViaCepClient
from .io_utils import looks_headerless, read_table, write_table_to_path
from .processor import (
    COL_DIAG,
    build_plan,
    detect_place_columns,
    estimate_work,
    format_duration,
    sanitize,
)

MIN_INTERVAL = 0.25  # piso de segurança
COLUNAS_PADRAO = "CEP,Cidade,UF,Bairro,Usado"


def _interval(value: str) -> float:
    number = float(value)
    if number < MIN_INTERVAL:
        raise argparse.ArgumentTypeError(
            f"intervalo mínimo é {MIN_INTERVAL}s (o ViaCEP bloqueia uso massivo)"
        )
    return number


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cep-sanitizer",
        description="Preenche Cidade e Estado a partir do CEP, consultando o ViaCEP com cuidado.",
    )
    p.add_argument("entrada", help="planilha .csv ou .xlsx")
    p.add_argument("-o", "--saida", help="arquivo de saída (padrão: <entrada>_sanitizado.<ext>)")
    p.add_argument("--coluna-cep", help="nome da coluna do CEP (padrão: detecta uma coluna 'CEP')")
    p.add_argument("--sem-cabecalho", action="store_true", help="a planilha não tem linha de cabeçalho")
    p.add_argument(
        "--colunas", default=COLUNAS_PADRAO,
        help=f"nomes das colunas, separados por vírgula, usados com --sem-cabecalho (padrão: {COLUNAS_PADRAO})",
    )
    p.add_argument("--coluna-cidade", help="coluna da Cidade da base, para comparar (padrão: detecta 'Cidade')")
    p.add_argument("--coluna-uf", help="coluna da UF da base, para comparar (padrão: detecta 'UF')")
    p.add_argument("--sem-comparacao", action="store_true", help="não compara com a base e não cria a coluna Status")
    p.add_argument("--filtro-coluna", help="coluna usada para filtrar (ex.: Usado, Situação)")
    p.add_argument("--filtro-valor", action="append", help="consulta SÓ linhas com este valor (ex.: 'Em uso')")
    p.add_argument("--filtro-excluir", action="append", help="NÃO consulta linhas com este valor (ex.: 0)")
    p.add_argument("--filtro-maior-que", type=float, help="consulta SÓ linhas com valor numérico maior que N")
    p.add_argument(
        "--completar-zero", action="store_true",
        help=(
            "CEP de 7 dígitos recebe zero à esquerda (use só se a planilha passou pelo Excel, que apaga "
            "o zero inicial). Sem esta opção, CEP de 7 dígitos é 'não encontrado': em bases exportadas "
            "de sistema ele costuma estar truncado, e completar com zero consultaria o CEP errado."
        ),
    )
    p.add_argument("--cache", help="arquivo de cache JSONL (padrão: <entrada>.cache_cep.jsonl)")
    p.add_argument("--intervalo", type=_interval, default=1.0, help="segundos entre consultas (padrão 1.0)")
    p.add_argument("--max-consultas", type=int, help="máximo de consultas NOVAS nesta execução")
    p.add_argument("--aba", help="aba do XLSX (padrão: primeira)")
    p.add_argument("--sep", help="separador do CSV (padrão: detecta)")
    p.add_argument("--encoding", help="codificação do CSV (padrão: detecta)")
    p.add_argument("--log-cada", type=int, default=100, help="mostra progresso a cada N consultas")
    p.add_argument("--dry-run", action="store_true", help="só estima o trabalho; não consulta")
    return p


def _detect_cep_column(columns) -> Optional[str]:
    lowered = {c: str(c).strip().casefold() for c in columns}
    for col, low in lowered.items():
        if low == "cep":
            return col
    for col, low in lowered.items():
        if "cep" in low:
            return col
    return None


def filter_rule(args) -> tuple[dict, str]:
    """Valida a combinação de opções de filtro; devolve (kwargs para build_plan, descrição)."""
    given = [
        args.filtro_valor is not None,
        args.filtro_excluir is not None,
        args.filtro_maior_que is not None,
    ]
    if not args.filtro_coluna:
        if any(given):
            raise ValueError("Informe --filtro-coluna junto com a regra do filtro.")
        return {}, "sem filtro (todas as linhas)"
    if sum(given) != 1:
        raise ValueError(
            "Com --filtro-coluna use exatamente uma regra: --filtro-valor, --filtro-excluir ou --filtro-maior-que."
        )
    col = args.filtro_coluna
    if args.filtro_valor is not None:
        return {"filter_values": args.filtro_valor}, f"{col} = {args.filtro_valor}"
    if args.filtro_excluir is not None:
        return {"exclude_values": args.filtro_excluir}, f"{col} diferente de {args.filtro_excluir}"
    return {"numeric_gt": args.filtro_maior_que}, f"{col} > {args.filtro_maior_que:g}"


def _comparison_columns(args, columns) -> tuple[Optional[str], Optional[str], str]:
    """Resolve as colunas de Cidade/UF da base. Devolve (cidade, uf, descrição)."""
    if args.sem_comparacao:
        return None, None, "desligada (--sem-comparacao): sem coluna Status"
    auto_city, auto_uf = detect_place_columns(columns)
    city = args.coluna_cidade or auto_city
    uf = args.coluna_uf or auto_uf
    for name in (city, uf):
        if name is not None and name not in columns:
            raise KeyError(f"Coluna não encontrada: {name!r}. Colunas disponíveis: {list(columns)}")
    if city is None or uf is None:
        return None, None, "indisponível (não achei colunas de Cidade e UF): sem coluna Status"
    return city, uf, f"Cidade da base={city!r}, UF da base={uf!r}"


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")

    entrada = Path(args.entrada)
    if not entrada.exists():
        print(f"Arquivo não encontrado: {entrada}", file=sys.stderr)
        return 1

    names = [c.strip() for c in args.colunas.split(",")] if args.sem_cabecalho else None
    try:
        df, meta = read_table(
            entrada, sheet=args.aba, sep=args.sep, encoding=args.encoding,
            header=not args.sem_cabecalho, names=names,
        )
    except Exception as exc:  # noqa: BLE001 - mensagem amigável para o usuário final
        print(f"Erro ao ler a planilha: {exc}", file=sys.stderr)
        return 1

    if not args.sem_cabecalho and looks_headerless(df.columns):
        print(
            "AVISO: os nomes de coluna lidos parecem dados (ex.: só dígitos). Se o arquivo NÃO tem "
            "cabeçalho, rode com --sem-cabecalho; senão o primeiro registro é perdido.",
            file=sys.stderr,
        )

    cep_col = args.coluna_cep or _detect_cep_column(df.columns)
    if cep_col is None or cep_col not in df.columns:
        print(f"Informe --coluna-cep. Colunas disponíveis: {list(df.columns)}", file=sys.stderr)
        return 1

    try:
        rule, rule_text = filter_rule(args)
        city_col, uf_col, compare_text = _comparison_columns(args, list(df.columns))
        plan = build_plan(
            df, cep_col, args.filtro_coluna,
            rule.get("filter_values"),
            exclude_values=rule.get("exclude_values"),
            numeric_gt=rule.get("numeric_gt"),
            pad_leading_zero=args.completar_zero,
        )
    except (KeyError, ValueError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1

    cache_path = Path(args.cache) if args.cache else entrada.with_name(entrada.name + ".cache_cep.jsonl")
    saida = Path(args.saida) if args.saida else entrada.with_name(f"{entrada.stem}_sanitizado{entrada.suffix}")

    with JournalCache(cache_path) as cache:
        work = estimate_work(plan, cache)
        per_query = args.intervalo * 1.1 + 0.3  # intervalo + jitter médio + latência típica
        to_do = work["to_query"] if args.max_consultas is None else min(work["to_query"], args.max_consultas)
        print(f"Linhas: {work['rows_total']:,} | coluna CEP: {cep_col!r} | filtro: {rule_text}")
        print(f"Comparação com a base: {compare_text}")
        print(
            f"Linhas no escopo: {work['rows_in_scope']:,} | fora do filtro (não consultadas): "
            f"{work['rows_out_of_scope']:,}"
        )
        if plan.filter_non_numeric:
            print(
                f"AVISO: {plan.filter_non_numeric:,} linhas têm valor vazio/não numérico em "
                f"{args.filtro_coluna!r} e ficaram FORA do escopo."
            )
        print(
            f"CEPs únicos válidos: {work['unique_ceps']:,} | já no cache: {work['cached']:,} "
            f"| a consultar: {work['to_query']:,}"
        )
        print(f"Consultas nesta execução: {to_do:,} | tempo estimado: {format_duration(to_do * per_query)}")
        if work["to_query"] > 5000:
            print(
                "AVISO: o ViaCEP avisa que uso massivo pode bloquear o acesso. Considere --max-consultas "
                "para fatiar a execução, e interrompa se houver erros 429."
            )
        if args.dry_run:
            return 0

        stop = {"flag": False}

        def _on_sigint(_signum, _frame):
            stop["flag"] = True
            signal.signal(signal.SIGINT, signal.SIG_DFL)  # segundo Ctrl+C encerra de vez
            print("\nInterrupção pedida: finalizando após a consulta atual e gravando a saída...")

        previous_handler = signal.signal(signal.SIGINT, _on_sigint)

        client = ViaCepClient(ClientConfig(min_interval=args.intervalo))
        started = time.monotonic()

        def _progress(done: int, total: int, _result) -> None:
            if done % max(1, args.log_cada) == 0 or done == total:
                rate = (time.monotonic() - started) / done
                print(f"  {done:,}/{total:,} consultas | restante ~{format_duration(rate * (total - done))}")

        try:
            out, stats = sanitize(
                df,
                cep_col,
                client,
                cache,
                city_col=city_col,
                uf_col=uf_col,
                max_new_lookups=args.max_consultas,
                progress=_progress,
                should_stop=lambda: stop["flag"],
                plan=plan,
            )
        finally:
            signal.signal(signal.SIGINT, previous_handler)

    write_table_to_path(out, meta, saida)
    print(f"\nSaída gravada em: {saida} ({len(out):,} linhas, todas as da entrada)")
    if args.sem_cabecalho:
        print(f"(A saída inclui uma linha de cabeçalho com os nomes: {', '.join(list(out.columns))})")
    print(f"Cache (progresso): {cache_path}")
    if stats.status_counts:
        print("Linhas por Status: " + ", ".join(f"{k}={v:,}" for k, v in sorted(stats.status_counts.items())))
    print("Linhas por diagnóstico: " + ", ".join(f"{k}={v:,}" for k, v in sorted(stats.diag_counts.items())))
    if stats.aborted_reason:
        print(f"ATENÇÃO - execução interrompida: {stats.aborted_reason}")
    if stats.pending_ceps:
        print(
            f"Faltam {stats.pending_ceps:,} CEPs únicos (linhas marcadas como PENDENTE em '{COL_DIAG}'). "
            "Rode o mesmo comando de novo para continuar de onde parou."
        )
    if stats.diag_counts.get("ERRO_REDE") or stats.diag_counts.get("ERRO_API"):
        print("Há linhas com ERRO_REDE/ERRO_API (falha transitória): rode de novo para refazê-las.")
    return 0 if stats.complete else 2


if __name__ == "__main__":
    sys.exit(main())
