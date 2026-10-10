"""Render the Markdown reports of the apply, check and fill commands."""

from dataclasses import dataclass
from pathlib import Path

from redline.apply import EditOutcome, PropertyChange
from redline.cite import CiteResult
from redline.figures import FigureFillResult
from redline.breaks import BreaksResult
from redline.fill import VALUES_FILE, FillResult, TableFillResult
from redline.headers import HeadersResult
from redline.italics import ItalicsResult
from redline.landscape import LandscapeResult
from redline.lists import RestartResult
from redline.markup import format_table_row
from redline.matcher import IndexedParagraph
from redline.prep import LANDSCAPE_CAPTIONS, MissingToken, PrepResult, centimetres, landscape_command
from redline.registry import RegistryBuild
from redline.validation import ValidationResult

EXCERPT_LIMIT = 70
VALIDATION_OUTPUT_LINES = 60


@dataclass(frozen=True)
class RunInfo:
    """What was run: command (apply or check), inputs, author, date and output path."""

    command: str
    docx: str
    edit_files: list[str]
    author: str
    date: str
    output: str | None = None


def render_edit_report(info: RunInfo, outcomes: list[EditOutcome], validation: ValidationResult | None) -> str:
    """Render the report of an apply or check run."""
    ok_label = "applied" if info.command == "apply" else "ok"
    lines = [f"# Redline {info.command} report", ""]
    lines += [f"- Document: `{info.docx}`", f"- Edit files: {', '.join(f'`{name}`' for name in info.edit_files)}"]
    lines += [f"- Author: {info.author}", f"- Run date (UTC): {info.date}"]
    if info.output:
        lines.append(f"- Output: `{info.output}`")
    lines += ["", "## Summary", ""]
    lines += _summary_table(info.edit_files, outcomes, ok_label.capitalize())
    lines += ["", "## Anchor failures", ""]
    lines += _anchor_failure_table(outcomes)
    lines += ["", "## Other errors", ""]
    lines += _other_error_table(outcomes)
    lines += ["", "## Warnings", ""]
    lines += _warning_table(outcomes)
    lines += ["", "## Edits", ""]
    lines += _edit_table(outcomes, ok_label)
    if validation is not None:
        lines += ["", "## validate.py", ""] + _validation_lines(validation)
    return "\n".join(lines) + "\n"


def render_fill_report(
    docx: str,
    values: str,
    output: str,
    result: FillResult,
    tables: TableFillResult | None = None,
    figures: FigureFillResult | None = None,
) -> str:
    """Render the report of a fill run."""
    lines = ["# Redline fill report", "", f"- Document: `{docx}`", f"- Values: `{values}`", f"- Output: `{output}`"]
    if tables is not None:
        lines += ["", "## Table tokens", ""] + _table_token_lines(tables)
    if figures is not None:
        lines += ["", "## Figure tokens", ""] + _figure_token_lines(figures)
    lines += ["", "## Filled tokens", ""] + _count_table(result.filled)
    lines += ["", "## Tokens without values", ""] + _count_table(result.missing_values)
    lines += ["", "## Values without tokens", ""]
    lines += [f"- {name}" for name in result.unused_values] or ["None."]
    lines += ["", "## Tokens outside tracked insertions (not filled)", ""] + _count_table(result.outside_insertions)
    return "\n".join(lines) + "\n"


def render_cite_report(info: RunInfo, registry: str, result: CiteResult) -> str:
    """Render the report of a cite run: counts, composition, the old-to-new map and problems."""
    if result.heading is None:
        heading = "not found"
    else:
        heading = f"{result.heading} ({result.old_entries_deleted} old entries deleted)"
    lines = ["# Redline cite report", "", f"- Document: `{info.docx}`", f"- Registry: `{registry}`", f"- Output: `{info.output}`"]
    lines += [f"- Author: {info.author}", f"- Run date (UTC): {info.date}", f"- Reference list heading: {heading}"]
    lines += ["", "## Markers", "", _row("Found", "Unresolved", "Rewritten (tracked)", "Rewritten inside insertions", "Unchanged")]
    lines += ["|---|---|---|---|---|", _row(result.markers_found, len(result.unresolved), result.rewritten_tracked, result.rewritten_in_insertions, result.unchanged)]
    composition = result.composition
    lines += ["", "## Composition of the main list", "", _row("Total", "Peer-reviewed", "Share", "Dated 2021-2026", "Share"), "|---|---|---|---|---|"]
    lines.append(_row(composition.total, composition.peer_reviewed, _share(composition.peer_reviewed, composition.total), composition.recent, _share(composition.recent, composition.total)))
    lines += ["", "## Numbering (old to new)", "", _row("New", "Record", "Old markers", "First appearance"), "|---|---|---|---|"]
    for record in result.main_list + result.technical_list:
        record_id = record.record_id
        lines.append(_row(result.labels[record_id], record_id, ", ".join(result.old_markers[record_id]), result.first_seen[record_id]))
    lines += ["", "## Unresolved markers (errors)", ""]
    unresolved = [_row(marker, excerpt) for marker, excerpt in result.unresolved]
    lines += [_row("Marker", "Paragraph"), "|---|---|"] + unresolved if unresolved else ["None."]
    lines += ["", "## Other errors", ""] + ([f"- {error}" for error in result.errors] or ["None."])
    lines += ["", "## Records never cited (warnings)", ""] + ([f"- {record_id}" for record_id in result.never_cited] or ["None."])
    return "\n".join(lines) + "\n"


def render_italics_report(info: RunInfo, terms: str, term_count: int, result: ItalicsResult, tracked_changes: int) -> str:
    """Render the report of an italics run: per scope and term counts, unfound terms and notes."""
    lines = ["# Redline italics report", "", f"- Document: `{info.docx}`", f"- Terms: `{terms}` ({term_count} terms)", f"- Output: `{info.output}`"]
    lines += [f"- Author: {info.author}", f"- Run date (UTC): {info.date}", f"- Tracked formatting changes: {tracked_changes}"]
    for scope in result.scopes:
        lines += ["", f"## {scope.name}", "", f"Paragraphs counted: {scope.paragraphs}", ""]
        header = _row("Term", "Occurrences", "Italicised", "Made upright", "Already correct", "Skipped", "First use")
        lines += [header, "|---|---|---|---|---|---|---|"]
        for name, use in sorted(scope.terms.items()):
            lines.append(_row(name, use.occurrences, use.italicised, use.upright, use.unchanged, use.skipped, use.first_excerpt))
    lines += ["", "## Terms found in neither scope", ""] + ([f"- {term}" for term in result.unfound_terms] or ["None."])
    lines += ["", "## Notes", ""] + ([f"- {note}" for note in result.notes] or ["None."])
    return "\n".join(lines) + "\n"


def render_headers_report(info: RunInfo, result: HeadersResult) -> str:
    """Render the report of a headers run: one row per table with the action taken."""
    lines = ["# Redline headers report", "", f"- Document: `{info.docx}`", f"- Output: `{info.output}`", f"- Author: {info.author}"]
    lines += [f"- Run date (UTC): {info.date}", f"- Tables given a repeating header row: {result.marked}", ""]
    lines += [_row("Table", "Rows", "First cell", "Action"), "|---|---|---|---|"]
    lines += [_row(outcome.number, outcome.rows, outcome.first_cell, outcome.action) for outcome in result.tables]
    return "\n".join(lines) + "\n"


def render_breaks_report(info: RunInfo, result: BreaksResult) -> str:
    """Render the report of a breaks run: each removed page break with the text before and after it."""
    lines = ["# Redline breaks report", "", f"- Document: `{info.docx}`", f"- Output: `{info.output}`", f"- Author: {info.author}"]
    lines += [f"- Run date (UTC): {info.date}", f"- Page breaks removed: {len(result.removals)}", ""]
    rows = [_row(removal.before, removal.after) for removal in result.removals]
    lines += [_row("Text before", "Text after"), "|---|---|"] + rows if rows else ["None."]
    return "\n".join(lines) + "\n"


def render_lists_report(info: RunInfo, result: RestartResult) -> str:
    """Render the report of a restart-lists run: the restarted lists and the prefixes that failed."""
    lines = ["# Redline restart-lists report", "", f"- Document: `{info.docx}`", f"- Output: `{info.output}`", f"- Author: {info.author}"]
    lines += [f"- Run date (UTC): {info.date}", "", "## Restarted lists", ""]
    rows = [_row(restart.prefix, restart.old_num_id, restart.new_num_id, restart.paragraphs) for restart in result.restarts]
    lines += [_row("First item starts with", "Old numId", "New numId", "Items moved"), "|---|---|---|---|"] + rows if rows else ["None."]
    lines += ["", "## Problems", ""] + ([f"- {error}" for error in result.errors] or ["None."])
    return "\n".join(lines) + "\n"


def render_landscape_report(info: RunInfo, result: LandscapeResult) -> str:
    """Render the report of a landscape run: one row per prefix with its block, text widths and outcome, then the problems."""
    lines = ["# Redline landscape report", "", f"- Document: `{info.docx}`", f"- Output: `{info.output}`", f"- Author: {info.author}"]
    lines += [f"- Run date (UTC): {info.date}", f"- Blocks turned to landscape: {result.turned}", "", "## Blocks", ""]
    rows = []
    for block in result.blocks:
        elements = ", ".join(block.elements)
        rows.append(_row(block.prefix, block.caption, elements, _twips(block.width_before), _twips(block.width_after), block.outcome))
    header = _row("Prefix", "Caption", "Block", "Text width before (twips)", "Text width after (twips)", "Outcome")
    lines += [header, "|---|---|---|---|---|---|"] + rows if rows else ["None."]
    lines += ["", "## Problems", ""] + ([f"- {problem}" for problem in result.problems] or ["None."])
    return "\n".join(lines) + "\n"


def render_prep_report(layout: str, output: str, result: PrepResult) -> str:
    """Render the report of a prep run: values, tables, figures, the landscape command and the problems."""
    lines = ["# Redline prep report", "", f"- Layout: `{layout}`", f"- Results: `{result.results}`", f"- Output: `{output}`"]
    drafts = ", ".join(f"`{name}`" for name in result.drafts) or "none given"
    lines += [f"- Drafts checked for tokens: {drafts}"]
    lines += ["", "## Values", ""] + _prep_value_lines(result)
    lines += ["", "## Tables", ""] + _prep_table_lines(result)
    lines += ["", "## Figures", ""] + ([f"- {figure.token}: {figure.source}" for figure in result.figures] or ["None copied."])
    lines += ["", "## Landscape command", ""] + _landscape_command_lines(result.landscape_captions)
    lines += ["", "## Problems", ""] + ([f"- {problem}" for problem in result.problems] or ["None."])
    return "\n".join(lines) + "\n"


def _prep_value_lines(result: PrepResult) -> list[str]:
    """The number of values written, every uncomputed value with its reason, and the draft tokens without a value."""
    lines = [f"Values written to {VALUES_FILE}: {result.values_written}", "", f"Uncomputed by the harness ({len(result.uncomputed)}):", ""]
    lines += [f"- `{name}`: {reason}" for name, reason in result.uncomputed.items()] or ["None."]
    lines += ["", f"Draft tokens without a value ({len(result.missing_tokens)}):", ""]
    if not result.drafts:
        return lines + ["Not checked: no --drafts given."]
    return lines + ([_missing_token_line(token) for token in result.missing_tokens] or ["None."])


def _missing_token_line(token: MissingToken) -> str:
    """'- `name` (edit text; drafts): reason', or 'notes sections only' when no edit's Text uses the token."""
    where = "edit text" if token.in_edit_text else "notes sections only"
    return f"- `{token.name}` ({where}; {', '.join(token.drafts)}): {token.reason}"


def _prep_table_lines(result: PrepResult) -> list[str]:
    """One block per written table, then the manual tables with their notes."""
    lines: list[str] = []
    for table in result.tables:
        dropped = "; ".join(f"{column.name} ({column.reason})" for column in table.dropped) or "none"
        transposed = f"yes, each of the {table.source_rows} source rows is a column" if table.transposed else "no"
        lines += [f"### {table.token}: {table.caption}", "", f"- Source: {table.source}", f"- Rows: {table.rows}"]
        lines += [f"- Kept columns: {', '.join(table.kept)}", f"- Dropped columns: {dropped}", f"- Transposed: {transposed}"]
        lines += [f"- Needed width at 10 pt: {table.needed_twips} twips ({centimetres(table.needed_twips)} cm)", f"- Orientation: {table.orientation}", ""]
    lines += ["### Manual tables", ""]
    return lines + ([f"- {table.token} ({table.caption}): {table.note}" for table in result.manual] or ["None."])


def _landscape_command_lines(captions: list[str]) -> list[str]:
    """The captions written to landscape-captions.txt and the landscape command that turns them."""
    if not captions:
        return [f"No table needs landscape; {LANDSCAPE_CAPTIONS} is empty."]
    lines = [f"{LANDSCAPE_CAPTIONS}:", ""] + [f"- {caption}" for caption in captions]
    return lines + ["", "```text", landscape_command(captions), "```"]


def _twips(width: int | None) -> str:
    """A width in twips, or n/a when the section does not give one."""
    return "n/a" if width is None else str(width)


def _share(part: int, total: int) -> str:
    """A percentage with one decimal, or n/a for an empty list."""
    return f"{part / total * 100:.1f}%" if total else "n/a"


def render_registry_report(sources: list[str], output: str, build: RegistryBuild) -> str:
    """Render the report of a registry build: decisions, problems and the records."""
    peer_reviewed = sum(1 for record in build.records if record.record_type == "peer_reviewed")
    technical = sum(1 for record in build.records if record.technical)
    lines = ["# Reference registry report", "", f"- Sources: {', '.join(f'`{source}`' for source in sources)}", f"- Output: `{output}`"]
    lines.append(f"- Records: {len(build.records)} ({peer_reviewed} peer-reviewed, {technical} technical)")
    for title, items in (("Merges", build.merges), ("Text conflicts", build.conflicts), ("Excluded", build.excluded), ("Notes", build.notes), ("Errors", build.errors)):
        lines += ["", f"## {title}", ""] + ([f"- {item}" for item in items] or ["None."])
    lines += ["", "## Records", "", _row("Id", "Type", "Year", "Technical", "Aliases", "Unverified"), "|---|---|---|---|---|---|"]
    for record in build.records:
        year = record.year if record.year is not None else "n.d."
        technical_mark = "yes" if record.technical else ""
        lines.append(_row(record.record_id, record.record_type, year, technical_mark, ", ".join(record.aliases), ", ".join(record.unverified)))
    return "\n".join(lines) + "\n"


def _table_token_lines(tables: TableFillResult) -> list[str]:
    """Rows per table token (filled, no file, or the file error) and the files without a token."""
    rows = [_row(name, "filled") for name in tables.tables_filled]
    rows += [_row(name, "no file") for name in tables.tables_without_file]
    rows += [_row(name, error) for name, error in tables.errors.items()]
    lines = [_row("Token", "Result"), "|---|---|"] + rows if rows else ["None."]
    lines += ["", "Files without a token:", ""]
    lines += [f"- {name}" for name in tables.files_without_token] or ["None."]
    lines += ["", "Tables to review:", ""]
    lines += [f"- {name}: {note}" for name, note in tables.review.items()] or ["None."]
    return lines


def _figure_token_lines(figures: FigureFillResult) -> list[str]:
    """Rows per figure token (filled, no file, or the file error) and the files without a token."""
    rows = [_row(name, "filled") for name in figures.figures_filled]
    rows += [_row(name, "no file") for name in figures.tokens_without_file]
    rows += [_row(name, error) for name, error in figures.errors.items()]
    lines = [_row("Token", "Result"), "|---|---|"] + rows if rows else ["None."]
    lines += ["", "Figure files without a token:", ""]
    lines += [f"- {name}" for name in figures.files_without_token] or ["None."]
    return lines


def _row(*cells: object) -> str:
    """One Markdown table row with pipes and line breaks in cells escaped."""
    return format_table_row(cells)


def _has_match_problem(outcome: EditOutcome) -> bool:
    """True when the outcome failed to match its anchor."""
    return any(problem.kind == "match" for problem in outcome.problems)


def _summary_table(edit_files: list[str], outcomes: list[EditOutcome], ok_header: str) -> list[str]:
    """Per edit file and in total: edits, ok, anchor failures, other errors, w:ins and w:del."""
    header = _row("Edit file", "Edits", ok_header, "Anchor failures", "Other errors", "Warnings", "w:ins", "w:del")
    lines = [header, "|---|---|---|---|---|---|---|---|"]
    for name in edit_files:
        lines.append(_summary_row(Path(name).name, [outcome for outcome in outcomes if outcome.source == name]))
    lines.append(_summary_row("Total", outcomes))
    return lines


def _summary_row(label: str, outcomes: list[EditOutcome]) -> str:
    """One summary row for a group of outcomes."""
    ok = sum(1 for outcome in outcomes if outcome.status == "applied")
    anchor_failures = sum(1 for outcome in outcomes if _has_match_problem(outcome))
    other_errors = sum(1 for outcome in outcomes if outcome.problems and not _has_match_problem(outcome))
    warnings = sum(len(outcome.warnings) for outcome in outcomes)
    inserted = sum(outcome.inserted for outcome in outcomes)
    deleted = sum(outcome.deleted for outcome in outcomes)
    return _row(label, len(outcomes), ok, anchor_failures, other_errors, warnings, inserted, deleted)


def _anchor_failure_table(outcomes: list[EditOutcome]) -> list[str]:
    """Rows for every anchor that did not select exactly one paragraph."""
    rows = []
    for outcome in outcomes:
        for problem in outcome.problems:
            if problem.kind == "match":
                candidates = "; ".join(_paragraph_label(candidate, None) for candidate in problem.candidates) or "none"
                rows.append(_row(outcome.edit_id, _location(outcome), problem.message, candidates))
    if not rows:
        return ["None."]
    return [_row("Edit", "Location", "Reason", "Candidate paragraphs"), "|---|---|---|---|"] + rows


def _other_error_table(outcomes: list[EditOutcome]) -> list[str]:
    """Rows for every problem that is not an anchor match failure."""
    rows = []
    for outcome in outcomes:
        for problem in outcome.problems:
            if problem.kind != "match":
                rows.append(_row(outcome.edit_id, _location(outcome), problem.kind, problem.message))
    if not rows:
        return ["None."]
    return [_row("Edit", "Location", "Kind", "Message"), "|---|---|---|---|"] + rows


def _warning_table(outcomes: list[EditOutcome]) -> list[str]:
    """Rows for every warning: the edit applied, but something was ignored or fell back."""
    rows = [_row(outcome.edit_id, _location(outcome), warning) for outcome in outcomes for warning in outcome.warnings]
    if not rows:
        return ["None."]
    return [_row("Edit", "Location", "Warning"), "|---|---|---|"] + rows


def _edit_table(outcomes: list[EditOutcome], ok_label: str) -> list[str]:
    """One row per edit with status, matched paragraph, revision counts and notes."""
    lines = [_row("Edit", "Location", "Operation", "Status", "Matched paragraph", "w:ins", "w:del", "Notes")]
    lines.append("|---|---|---|---|---|---|---|---|")
    for outcome in outcomes:
        status = ok_label if outcome.status == "applied" else "error"
        matched = _paragraph_label(outcome.matched, outcome.matched_inserted_by) if outcome.matched else ""
        notes = []
        if outcome.matched_inserted_by:
            notes.append(f"anchor found only in text inserted by {outcome.matched_inserted_by}")
        notes += sorted({problem.kind for problem in outcome.problems})
        if outcome.warnings:
            notes.append("warning")
        if outcome.property_change is not None:
            notes.append(_property_change_note(outcome.property_change))
        operation = outcome.operation or "(unparsed)"
        lines.append(_row(outcome.edit_id, _location(outcome), operation, status, matched, outcome.inserted, outcome.deleted, "; ".join(notes)))
    return lines


def _property_change_note(change: PropertyChange) -> str:
    """'pPr changed (style A to B; numId x to y)', with 'none' for no style or no list numbering."""
    styles = f"style {change.old_style or 'none'} to {change.new_style or 'none'}"
    num_ids = f"numId {change.old_num_id or 'none'} to {change.new_num_id or 'none'}"
    return f"pPr changed ({styles}; {num_ids})"


def _validation_lines(validation: ValidationResult) -> list[str]:
    """The validate.py command, verdict and the tail of its output."""
    if not validation.ran:
        verdict = "NOT RUN"
    elif validation.passed:
        verdict = f"PASSED (exit {validation.exit_code})"
    else:
        verdict = f"FAILED (exit {validation.exit_code})"
    tail = "\n".join(validation.output.splitlines()[-VALIDATION_OUTPUT_LINES:])
    return [f"Command: `{validation.command}`", "", f"Result: {verdict}", "", "```text", tail, "```"]


def _count_table(counts: dict[str, int]) -> list[str]:
    """A token and occurrence table, or 'None.' when empty."""
    if not counts:
        return ["None."]
    return [_row("Token", "Occurrences"), "|---|---|"] + [_row(name, count) for name, count in counts.items()]


def _location(outcome: EditOutcome) -> str:
    """file.md:line of the edit block."""
    return f"{Path(outcome.source).name}:{outcome.line}"


def _paragraph_label(paragraph: IndexedParagraph, inserted_by: str | None) -> str:
    """'#n: excerpt', '#n (table): excerpt', or 'inserted by E-x: excerpt'."""
    excerpt = paragraph.text if len(paragraph.text) <= EXCERPT_LIMIT else paragraph.text[:EXCERPT_LIMIT] + "..."
    if inserted_by:
        return f"inserted by {inserted_by}: {excerpt}"
    where = f"#{paragraph.number}" + (" (table)" if paragraph.in_table else "")
    return f"{where}: {excerpt}"
