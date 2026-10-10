"""Command-line interface: python -m redline apply | check | prep | landscape | fill | registry | cite | italics | headers | restart-lists | breaks | terms.

Pipeline order (redline-spec.md): registry, prep, apply, landscape, fill, cite, italics, headers,
restart-lists, breaks. prep builds the fill folder from the harness results and lists the captions
of the tables that need landscape. landscape runs after apply, whose edits write the captions and
{{TABLE:name}} paragraphs it looks for, and before fill, which sizes each table for the text column
of the section that holds its token, so a table on a landscape page gets the landscape width.
"""

import argparse
import sys
from pathlib import Path

from redline.commands import (
    CommandError,
    run_apply,
    run_breaks,
    run_check,
    run_cite,
    run_fill,
    run_headers,
    run_italics,
    run_landscape,
    run_prep,
    run_registry,
    run_restart_lists,
    run_terms,
)
from redline.terms import DEFAULT_ADDITIONS_PATH, DEFAULT_EXCLUSIONS_PATH


def build_parser() -> argparse.ArgumentParser:
    """Define every subcommand and its options."""
    parser = argparse.ArgumentParser(prog="redline", description="Apply draft edit lists to a .docx as tracked changes.")
    commands = parser.add_subparsers(dest="command", required=True)

    apply = commands.add_parser("apply", help="apply edits as tracked changes and write the revised .docx")
    apply.add_argument("--docx", required=True, type=Path)
    apply.add_argument("--edits", required=True, nargs="+", action="extend", type=Path)
    apply.add_argument("--author", required=True)
    apply.add_argument("--out", required=True, type=Path)
    apply.add_argument("--report", required=True, type=Path)

    check = commands.add_parser("check", help="parse and match only; write nothing")
    check.add_argument("--edits", required=True, nargs="+", action="extend", type=Path)
    check.add_argument("--docx", required=True, type=Path)
    check.add_argument("--report", type=Path)

    prep = commands.add_parser("prep", help="build the fill folder from the harness results as the layout file says")
    prep.add_argument("--layout", required=True, type=Path, help="docs/thesis/fill-layout.json")
    prep.add_argument("--root", required=True, type=Path, help="repository root; the layout's paths are relative to it")
    prep.add_argument("--out", required=True, type=Path, help="the fill folder to write; it must not exist yet or be empty")
    prep.add_argument("--report", required=True, type=Path)
    prep.add_argument("--results", type=Path, help="a harness results folder to read instead of the layout's results_dir")
    prep.add_argument("--drafts", nargs="+", action="extend", default=[], type=Path, help="drafts whose DATA tokens must have a value")

    landscape = commands.add_parser("landscape", help="put each named caption and its wide table on a landscape page of its own")
    _add_tracked_arguments(landscape)
    landscape.add_argument("--at", required=True, nargs="+", action="extend", metavar="PREFIX", help="text the caption of a wide table starts with")

    fill = commands.add_parser("fill", help="fill {{TABLE:name}}, {{FIGURE:stem}} and {{DATA:name}} tokens")
    fill.add_argument("--docx", required=True, type=Path)
    fill.add_argument("--values", required=True, type=Path)
    fill.add_argument("--out", required=True, type=Path)
    fill.add_argument("--report", required=True, type=Path)
    fill.add_argument("--author", help="author of the tracked tables that {{TABLE:name}} tokens become")

    registry = commands.add_parser("registry", help="build the reference registry JSON from the drafts")
    registry.add_argument("--drafts", required=True, nargs="+", action="extend", type=Path)
    registry.add_argument("--out", required=True, type=Path)
    registry.add_argument("--report", required=True, type=Path)

    cite = commands.add_parser("cite", help="renumber citations and rebuild the reference list as tracked changes")
    cite.add_argument("--docx", required=True, type=Path)
    cite.add_argument("--registry", required=True, type=Path)
    cite.add_argument("--out", required=True, type=Path)
    cite.add_argument("--report", required=True, type=Path)
    cite.add_argument("--author", required=True, help="author of the tracked changes")

    italics = commands.add_parser("italics", help="italicise foreign terms at first use as tracked formatting changes")
    _add_tracked_arguments(italics)
    italics.add_argument("--terms", required=True, type=Path, help="one foreign term per line")

    headers = commands.add_parser("headers", help="repeat the first row of every multi-row table as a header row")
    _add_tracked_arguments(headers)
    headers.add_argument("--skip-first-cell", action="append", default=[], metavar="PREFIX", help="leave tables whose first cell starts with PREFIX alone")

    lists = commands.add_parser("restart-lists", help="restart numbered lists at 1")
    _add_tracked_arguments(lists)
    lists.add_argument("--at", required=True, nargs="+", action="extend", metavar="PREFIX", help="text the first item of a list starts with")

    breaks = commands.add_parser("breaks", help="delete a page break that sits right before a next-page section break")
    _add_tracked_arguments(breaks)

    terms = commands.add_parser("terms", help="build the foreign term list from BAB III italics and the drafts")
    terms.add_argument("--docx", required=True, type=Path)
    terms.add_argument("--drafts", required=True, nargs="+", action="extend", type=Path)
    terms.add_argument("--out", required=True, type=Path)
    terms.add_argument("--report", required=True, type=Path)
    terms.add_argument("--add", type=Path, default=DEFAULT_ADDITIONS_PATH, help="terms to add to the list, one 'term | reason' per line")
    terms.add_argument("--exclude", type=Path, default=DEFAULT_EXCLUSIONS_PATH, help="proper nouns and product names to drop")
    return parser


def _add_tracked_arguments(command: argparse.ArgumentParser) -> None:
    """The arguments every tracked-change pass takes: input, output, report and author."""
    command.add_argument("--docx", required=True, type=Path)
    command.add_argument("--out", required=True, type=Path)
    command.add_argument("--report", required=True, type=Path)
    command.add_argument("--author", required=True, help="author of the tracked changes")


def main(argv: list[str] | None = None) -> int:
    """Run one subcommand; exit 0 when everything succeeded, 1 when not, 2 on an expected failure."""
    args = build_parser().parse_args(argv)
    try:
        if args.command == "apply":
            result = run_apply(args.docx, args.edits, args.author, args.out)
        elif args.command == "check":
            result = run_check(args.docx, args.edits)
        elif args.command == "registry":
            result = run_registry(args.drafts, args.out)
        elif args.command == "cite":
            result = run_cite(args.docx, args.registry, args.out, args.author)
        elif args.command == "italics":
            result = run_italics(args.docx, args.terms, args.out, args.author)
        elif args.command == "headers":
            result = run_headers(args.docx, args.out, args.author, tuple(args.skip_first_cell))
        elif args.command == "restart-lists":
            result = run_restart_lists(args.docx, args.out, args.author, args.at)
        elif args.command == "breaks":
            result = run_breaks(args.docx, args.out, args.author)
        elif args.command == "landscape":
            result = run_landscape(args.docx, args.out, args.author, args.at)
        elif args.command == "prep":
            result = run_prep(args.layout, args.root, args.out, args.results, args.drafts)
        elif args.command == "terms":
            result = run_terms(args.docx, args.drafts, args.out, args.exclude, args.add)
        else:
            result = run_fill(args.docx, args.values, args.out, args.author)
    except CommandError as error:
        print(f"redline: {error}", file=sys.stderr)
        return 2
    if args.report:
        args.report.write_text(result.report, encoding="utf-8")
    if args.command == "check":
        print(result.report, end="")
    else:
        print(f"redline: wrote {args.out}; report {args.report}")
    return 0 if result.ok else 1
