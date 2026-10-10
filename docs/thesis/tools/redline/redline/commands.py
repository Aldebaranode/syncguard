"""The three command flows: apply, check and fill. Each returns its report text and a verdict.

These functions own the work (loading, applying, saving, validating, rendering); the CLI only
parses arguments, calls one of them, writes the report and maps the verdict to an exit code.
"""

import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from redline.apply import run_edits
from redline.cite import cite_document
from redline.figures import fill_figure_tokens, figure_files, figure_token_names
from redline.fill import FillError, fill_table_tokens, fill_tokens, load_values_source, table_token_names
from redline.breaks import remove_redundant_page_breaks
from redline.headers import mark_header_rows
from redline.italics import italicise_document, load_terms
from redline.landscape import turn_landscape
from redline.lists import restart_lists
from redline.package import PackageError, WordPackage
from redline.prep import PrepError, prepare
from redline.registry import RegistryError, build_registry, load_registry, write_registry
from redline.report import (
    RunInfo,
    render_cite_report,
    render_breaks_report,
    render_edit_report,
    render_fill_report,
    render_headers_report,
    render_italics_report,
    render_landscape_report,
    render_lists_report,
    render_prep_report,
    render_registry_report,
)
from redline.structure import paragraph_style_names
from redline.terms import DEFAULT_EXCLUSIONS_PATH, add_additions, collect_term_evidence, load_additions, load_exclusions, render_terms_report, select_terms
from redline.tracked import RevisionMarks
from redline.validation import run_validate

CHECK_AUTHOR = "redline check"
STYLES_PART = "word/styles.xml"
NUMBERING_PART = "word/numbering.xml"
EXPECTED_FAILURES = (PackageError, FillError, RegistryError, PrepError, OSError, zipfile.BadZipFile)


class CommandError(Exception):
    """An expected failure that stops a command before it produces a report."""


@dataclass(frozen=True)
class CommandResult:
    """The rendered report and whether everything succeeded."""

    report: str
    ok: bool


def utc_now() -> str:
    """The current time in ISO 8601 UTC, to the second."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_apply(docx: Path, edit_paths: list[Path], author: str, out: Path, date: str | None = None, validate: bool = True) -> CommandResult:
    """Apply the drafts to docx as tracked changes, save out, validate it, and report."""
    _refuse_overwrite(docx, out)
    date = date or utc_now()
    try:
        package = WordPackage.load(docx)
        outcomes = run_edits(package, edit_paths, author, date)
        package.save(out)
    except EXPECTED_FAILURES as error:
        raise CommandError(str(error)) from error
    validation = run_validate(out, docx, author) if validate else None
    info = RunInfo("apply", str(docx), [str(path) for path in edit_paths], author, date, str(out))
    all_applied = all(outcome.status == "applied" for outcome in outcomes)
    validation_ok = validation is None or validation.passed
    return CommandResult(render_edit_report(info, outcomes, validation), all_applied and validation_ok)


def run_check(docx: Path, edit_paths: list[Path]) -> CommandResult:
    """Parse the drafts and match every anchor against docx in memory; nothing is written."""
    date = utc_now()
    try:
        outcomes = run_edits(WordPackage.load(docx), edit_paths, CHECK_AUTHOR, date)
    except EXPECTED_FAILURES as error:
        raise CommandError(str(error)) from error
    info = RunInfo("check", str(docx), [str(path) for path in edit_paths], "not used (check writes nothing)", date)
    all_ok = all(outcome.status == "applied" for outcome in outcomes)
    return CommandResult(render_edit_report(info, outcomes, None), all_ok)


def run_fill(docx: Path, values_path: Path, out: Path, author: str | None = None) -> CommandResult:
    """Fill TABLE, FIGURE then DATA tokens and save out; ok when no token is left unfilled."""
    _refuse_overwrite(docx, out)
    try:
        values, table_files = load_values_source(values_path)
        package = WordPackage.load(docx)
        figure_paths = figure_files(values_path)
        fills_tables = bool(set(table_token_names(package.document)) & set(table_files))
        fills_figures = bool(set(figure_token_names(package.document)) & set(figure_paths))
        if (fills_tables or fills_figures) and author is None:
            raise CommandError("filling table and figure tokens inserts tracked changes, which needs --author")
        # Without an author nothing tracked is inserted (checked above), so these marks stay unused.
        marks = RevisionMarks.for_document(package.document, author or "", utc_now())
        tables = fill_table_tokens(package.document, table_files, marks)
        figures = fill_figure_tokens(package, figure_paths, marks)
        result = fill_tokens(package.document, values)
        package.save(out)
    except EXPECTED_FAILURES as error:
        raise CommandError(str(error)) from error
    report = render_fill_report(str(docx), str(values_path), str(out), result, tables, figures)
    all_filled = not (result.missing_values or tables.tables_without_file or tables.errors or figures.tokens_without_file or figures.errors)
    return CommandResult(report, all_filled)


def run_registry(draft_paths: list[Path], out: Path) -> CommandResult:
    """Build the reference registry from the drafts and write it as JSON; ok when nothing is unresolved."""
    try:
        drafts = {str(path): Path(path).read_text(encoding="utf-8") for path in draft_paths}
        build = build_registry(drafts)
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        write_registry(build.records, out)
    except EXPECTED_FAILURES as error:
        raise CommandError(str(error)) from error
    report = render_registry_report([str(path) for path in draft_paths], str(out), build)
    return CommandResult(report, not build.errors)


def run_cite(docx: Path, registry_path: Path, out: Path, author: str) -> CommandResult:
    """Renumber citations and rebuild the reference list as tracked changes; ok when every marker resolved."""
    _refuse_overwrite(docx, out)
    date = utc_now()
    try:
        registry = load_registry(registry_path)
        package = WordPackage.load(docx)
        result = cite_document(package.document, registry, RevisionMarks.for_document(package.document, author, date))
        package.save(out)
    except EXPECTED_FAILURES as error:
        raise CommandError(str(error)) from error
    info = RunInfo("cite", str(docx), [], author, date, str(out))
    return CommandResult(render_cite_report(info, str(registry_path), result), not result.unresolved and not result.errors)


def run_italics(docx: Path, terms_path: Path, out: Path, author: str) -> CommandResult:
    """Italicise foreign terms at first use as tracked formatting changes; ok when both scopes were found."""
    _refuse_overwrite(docx, out)
    date = utc_now()
    try:
        terms = load_terms(terms_path)
        package = WordPackage.load(docx)
        style_names = paragraph_style_names(package.read_part(STYLES_PART))
        marks = RevisionMarks.for_document(package.document, author, date)
        result = italicise_document(package.document, style_names, terms, marks)
        package.save(out)
    except EXPECTED_FAILURES as error:
        raise CommandError(str(error)) from error
    info = RunInfo("italics", str(docx), [], author, date, str(out))
    return CommandResult(render_italics_report(info, str(terms_path), len(terms), result, marks.property_changed_count), not result.notes)


def run_headers(docx: Path, out: Path, author: str, skip_first_cells: tuple[str, ...] = ()) -> CommandResult:
    """Repeat the first row of every multi-row table as tracked changes; ok when no table hit a conflict."""
    _refuse_overwrite(docx, out)
    date = utc_now()
    try:
        package = WordPackage.load(docx)
        marks = RevisionMarks.for_document(package.document, author, date)
        result = mark_header_rows(package.body, marks, skip_first_cells)
        package.save(out)
    except EXPECTED_FAILURES as error:
        raise CommandError(str(error)) from error
    info = RunInfo("headers", str(docx), [], author, date, str(out))
    conflicts = any(outcome.action.startswith("has an earlier") for outcome in result.tables)
    return CommandResult(render_headers_report(info, result), not conflicts)


def run_breaks(docx: Path, out: Path, author: str) -> CommandResult:
    """Delete, tracked, page breaks that sit right before a next-page section break."""
    _refuse_overwrite(docx, out)
    date = utc_now()
    try:
        package = WordPackage.load(docx)
        marks = RevisionMarks.for_document(package.document, author, date)
        result = remove_redundant_page_breaks(package.body, marks)
        package.save(out)
    except EXPECTED_FAILURES as error:
        raise CommandError(str(error)) from error
    info = RunInfo("breaks", str(docx), [], author, date, str(out))
    return CommandResult(render_breaks_report(info, result), True)


def run_restart_lists(docx: Path, out: Path, author: str, prefixes: list[str]) -> CommandResult:
    """Restart the numbered lists that begin at the prefixes; ok when every prefix was used."""
    _refuse_overwrite(docx, out)
    date = utc_now()
    try:
        package = WordPackage.load(docx)
        numbering = package.read_part(NUMBERING_PART)
        style_names = paragraph_style_names(package.read_part(STYLES_PART))
        marks = RevisionMarks.for_document(package.document, author, date)
        result = restart_lists(package.document, numbering, style_names, prefixes, marks)
        package.set_xml_part(NUMBERING_PART, numbering)
        package.save(out)
    except EXPECTED_FAILURES as error:
        raise CommandError(str(error)) from error
    info = RunInfo("restart-lists", str(docx), [], author, date, str(out))
    return CommandResult(render_lists_report(info, result), not result.errors)


def run_landscape(docx: Path, out: Path, author: str, prefixes: list[str]) -> CommandResult:
    """Put the caption and table block of each prefix on a landscape page; ok when every prefix turned."""
    _refuse_overwrite(docx, out)
    date = utc_now()
    try:
        package = WordPackage.load(docx)
        marks = RevisionMarks.for_document(package.document, author, date)
        result = turn_landscape(package.body, prefixes, marks)
        package.save(out)
    except EXPECTED_FAILURES as error:
        raise CommandError(str(error)) from error
    info = RunInfo("landscape", str(docx), [], author, date, str(out))
    return CommandResult(render_landscape_report(info, result), not result.problems)


def run_prep(layout: Path, root: Path, out: Path, results: Path | None = None, drafts: list[Path] | None = None) -> CommandResult:
    """Build the fill folder from the harness results as the layout says; ok when no problem was found."""
    try:
        result = prepare(layout, root, out, results, drafts or [])
    except EXPECTED_FAILURES as error:
        raise CommandError(str(error)) from error
    return CommandResult(render_prep_report(str(layout), str(out), result), result.ok)


def run_terms(
    docx: Path,
    draft_paths: list[Path],
    out: Path,
    exclusions_path: Path = DEFAULT_EXCLUSIONS_PATH,
    additions_path: Path | None = None,
) -> CommandResult:
    """Write the foreign term list built from BAB III italics and the drafts' starred terms, with its evidence report."""
    try:
        package = WordPackage.load(docx)
        drafts = {str(path): Path(path).read_text(encoding="utf-8") for path in draft_paths}
        evidence = collect_term_evidence(package.body, drafts)
        if additions_path is not None and Path(additions_path).is_file():
            add_additions(evidence, load_additions(additions_path))
        selection = select_terms(evidence, load_exclusions(exclusions_path))
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text("".join(f"{item.term}\n" for item in selection.kept), encoding="utf-8")
    except EXPECTED_FAILURES as error:
        raise CommandError(str(error)) from error
    return CommandResult(render_terms_report(selection, str(out)), True)


def _refuse_overwrite(docx: Path, out: Path) -> None:
    """Never write over the input document."""
    if Path(out).resolve() == Path(docx).resolve():
        raise CommandError(f"refusing to overwrite the input document {docx}; choose another --out")
