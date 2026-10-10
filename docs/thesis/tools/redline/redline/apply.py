"""Apply parsed edits to a loaded document as tracked changes, one outcome per edit.

Entry points: run_edits() parses the draft files and applies them (the flow behind both the
apply and the check command); apply_edits() applies already-parsed edits. Matching always runs
against the original document; only when a paragraph anchor has no match there are paragraphs
inserted by earlier edits searched, and the outcome names the edit that inserted it. An edit with
any problem changes nothing; the other edits still apply. Warnings never stop an edit.

Anchors are paragraph text, 'ROW: <cell or row>' (with an optional 'Table:' line), or
'TABLE: <first cell or first row>'; see table_index.py for the table forms.

A Numbering key names a list item by a prefix matched like an anchor (original paragraphs first,
then inserted ones); its list numbering goes to the edit's paragraphs. A replace records a change
of its paragraph's w:pPr in a w:pPrChange.
"""

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

from redline.edits import NUMBERING_NEW, NUMBERING_NONE, ROW_ANCHOR, TABLE_ANCHOR, Edit, ParseProblem, load_edit_files
from redline.lists import add_restarted_num, find_num, numbering_properties, paragraph_level, paragraph_num_id
from redline.markup import Block, MarkupError, Row, TableBlock, parse_row_lines, parse_text_blocks
from redline.matcher import AnchorError, IndexedParagraph, index_paragraphs, match_anchor, paragraph_text
from redline.package import WordPackage
from redline.rows import build_inserted_row, delete_row, delete_table, replace_row_cells
from redline.structure import body_font_name, column_width_or_none, find_image_paragraph
from redline.figures import FIGURE_TOKEN
from redline.styles import ResolvedStyle, StyleCatalog, StyleNotFound, load_aliases
from redline.table_index import IndexedRow, IndexedTable, index_tables, match_row, match_table
from redline.tables import build_inserted_table, nearest_table_before
from redline.tracked import (
    RevisionMarks,
    append_inserted_runs,
    build_inserted_paragraph,
    can_track_paragraph_change,
    copied_paragraph_properties,
    delete_runs,
    figure_paragraph_properties,
    first_text_run_properties,
    mark_paragraph_deleted,
    open_paragraph_change,
    set_paragraph_property,
    styled_paragraph_properties,
)
from redline.wordml import make_element, qn, remove_children

STYLES_PART = "word/styles.xml"
NUMBERING_PART = "word/numbering.xml"
ESTIMATE_FONT = "Times New Roman"
INSERT_OPERATIONS = ("insert_after", "insert_before")
CHANGING_OPERATIONS = ("replace", "delete")
NO_TABLE_MESSAGE = "the document has no existing table to copy table properties from"
PROPERTY_CHANGE_CONFLICT = "the paragraph already holds a tracked paragraph property change (w:pPrChange), and a w:pPr holds only one"
STYLE_IGNORED = "Style ignored, paragraph properties kept"


@dataclass(frozen=True)
class Problem:
    """Why an edit was not applied. kind: parse, match, rule, markup, table, figure, numbering or conflict."""

    kind: str
    message: str
    candidates: tuple[IndexedParagraph, ...] = ()


@dataclass(frozen=True)
class PropertyChange:
    """A replace's change of its paragraph's style id and list numId; a numId of None means no list numbering."""

    old_style: str | None
    old_num_id: str | None
    new_style: str | None
    new_num_id: str | None


@dataclass
class EditOutcome:
    """What happened to one edit: matched paragraph, problems, warnings, revision counts and any w:pPr change."""

    edit_id: str | None
    source: str
    line: int
    operation: str
    problems: list[Problem] = field(default_factory=list)
    matched: IndexedParagraph | None = None
    matched_inserted_by: str | None = None
    inserted: int = 0
    deleted: int = 0
    warnings: list[str] = field(default_factory=list)
    property_change: PropertyChange | None = None

    @property
    def status(self) -> str:
        """'applied' when the edit has no problems, otherwise 'error'."""
        return "error" if self.problems else "applied"


@dataclass
class _Template:
    """Paragraph and run properties that new paragraphs of one edit start from."""

    paragraph: etree._Element | None
    run: etree._Element | None

    def paragraph_copy(self) -> etree._Element | None:
        """A fresh copy of the paragraph properties for one new paragraph."""
        return deepcopy(self.paragraph) if self.paragraph is not None else None

    def with_numbering(self, numbering: etree._Element | None) -> "_Template":
        """A copy whose paragraph properties carry this w:numPr (None: no list numbering)."""
        properties = self.paragraph_copy()
        if properties is None:
            properties = make_element("pPr")
        set_paragraph_property(properties, "numPr", numbering)
        return _Template(properties, self.run)


@dataclass(frozen=True)
class _ListPlan:
    """A Numbering key resolved against the document: its mode and, for continue and new, the named item's numId and level."""

    mode: str
    num_id: str | None = None
    level: str | None = None


@dataclass(frozen=True)
class _Restyle:
    """A replace's planned change of its paragraph's w:pPr; a field left None keeps that property as it is."""

    style_id: str | None
    numbering: _ListPlan | None


def run_edits(package: WordPackage, edit_paths: list[Path], author: str, date: str) -> list[EditOutcome]:
    """Parse the draft files, apply their edits, and return all outcomes in file and line order."""
    parsed = load_edit_files(edit_paths)
    outcomes = apply_edits(package, parsed.edits, author, date) + _parse_outcomes(parsed.problems)
    file_order = {str(path): index for index, path in enumerate(edit_paths)}
    outcomes.sort(key=lambda outcome: (file_order.get(outcome.source, len(file_order)), outcome.line))
    return outcomes


def apply_edits(package: WordPackage, edits: list[Edit], author: str, date: str) -> list[EditOutcome]:
    """Apply edits in order to the package's document tree and return one outcome per edit."""
    session = _Session(package, author, date)
    return [session.apply(edit) for edit in edits]


def _parse_outcomes(problems: list[ParseProblem]) -> list[EditOutcome]:
    """Group parse problems by edit block into error outcomes."""
    grouped: dict[tuple[str, str | None], EditOutcome] = {}
    for problem in problems:
        key = (problem.source, problem.edit_id)
        if key not in grouped:
            grouped[key] = EditOutcome(problem.edit_id, problem.source, problem.line, "")
        outcome = grouped[key]
        outcome.line = min(outcome.line, problem.line)
        outcome.problems.append(Problem("parse", f"line {problem.line}: {problem.message}"))
    return list(grouped.values())


class _Session:
    """State shared by the edits of one run: indexes, revision marks, and placement cursors."""

    def __init__(self, package: WordPackage, author: str, date: str):
        """Index the original paragraphs and tables and prepare the revision marks."""
        self._package = package
        self._body = package.body
        self._original = index_paragraphs(self._body)
        self._tables = index_tables(self._body, self._original)
        self._original_table_ids = {id(table.element) for table in self._tables}
        self._inserted: list[IndexedParagraph] = []
        self._inserted_by: dict[int, str] = {}
        self._changed_by: dict[int, str] = {}
        self._last_after: dict[int, etree._Element] = {}
        self._marks = RevisionMarks.for_document(package.document, author, date)
        self._styles: StyleCatalog | None = None
        self._numbering: etree._Element | None = None
        self._table_notes: list[str] = []

    def apply(self, edit: Edit) -> EditOutcome:
        """Check one edit and, when it has no problems, apply it as tracked changes."""
        outcome = EditOutcome(edit.edit_id, edit.source, edit.line, edit.operation)
        inserted_before, deleted_before = self._marks.inserted_count, self._marks.deleted_count
        if edit.anchor.startswith((ROW_ANCHOR, TABLE_ANCHOR)):
            self._apply_table_edit(edit, outcome)
        else:
            self._apply_paragraph_edit(edit, outcome)
        outcome.warnings.extend(self._table_notes)
        self._table_notes = []
        outcome.inserted = self._marks.inserted_count - inserted_before
        outcome.deleted = self._marks.deleted_count - deleted_before
        return outcome

    # Paragraph anchors

    def _apply_paragraph_edit(self, edit: Edit, outcome: EditOutcome) -> None:
        """Check and run a replace, delete or insert anchored on a paragraph."""
        if edit.operation == "replace_image":
            self._apply_replace_image(edit, outcome)
            return
        target = self._locate(edit, outcome)
        blocks = self._text_blocks(edit, outcome)
        list_plan = self._list_plan(edit, outcome)
        restyle = None
        if target is not None:
            self._check_conflicts(edit, target.element, outcome)
            restyle = self._plan_restyle(edit, target.element, list_plan, outcome)
        starts_with_table = bool(blocks) and isinstance(blocks[0], TableBlock)
        if outcome.matched_inserted_by is not None and edit.operation == "replace" and starts_with_table:
            message = f"replace with a table of text inserted by {outcome.matched_inserted_by} is not supported"
            outcome.problems.append(Problem("conflict", message))
        if outcome.problems:
            return
        paragraph = target.element
        if restyle is not None:
            self._restyle(paragraph, restyle, outcome)
        template = self._template(edit, paragraph, list_plan, outcome)
        if edit.operation == "replace" and outcome.matched_inserted_by is not None:
            self._replace_inserted_text(target, blocks, template, edit.edit_id)
        elif edit.operation == "replace":
            self._replace_paragraph(target, blocks, template, edit.edit_id)
        elif edit.operation == "delete":
            self._warn_about_style_on_delete(edit, paragraph, outcome)
            delete_runs(paragraph, self._marks)
            mark_paragraph_deleted(paragraph, self._marks)
        elif edit.operation == "insert_after":
            self._insert_after(paragraph, self._build(blocks, paragraph, template, edit.edit_id))
        else:
            for element in self._build(blocks, paragraph, template, edit.edit_id):
                paragraph.addprevious(element)
        if edit.operation in CHANGING_OPERATIONS:
            self._changed_by[id(paragraph)] = edit.edit_id

    def _apply_replace_image(self, edit: Edit, outcome: EditOutcome) -> None:
        """Delete the picture above the caption as a tracked change and insert the Text token in its paragraph."""
        caption = self._locate(edit, outcome)
        if caption is None:
            return
        image = find_image_paragraph(caption.element)
        if image is None:
            message = "no image paragraph directly above the caption (one empty paragraph may sit between them)"
            outcome.problems.append(Problem("figure", message))
            return
        earlier = self._changed_by.get(id(image))
        if earlier is not None:
            outcome.problems.append(Problem("conflict", f"already changed by {earlier}"))
            return
        blocks = self._text_blocks(edit, outcome)
        if outcome.problems:
            return
        if not FIGURE_TOKEN.fullmatch(" ".join(segment.text for segment in blocks[0].segments)):
            outcome.warnings.append("Text is not a {{FIGURE:name}} token, so fill will not place a picture")
        insert_point = delete_runs(image, self._marks)
        append_inserted_runs(image, blocks[0].segments, None, self._marks, after=insert_point)
        self._changed_by[id(image)] = edit.edit_id

    def _replace_paragraph(self, target: IndexedParagraph, blocks: list[Block], template: _Template, edit_id: str) -> None:
        """Swap the paragraph's text; when Text starts with a table, delete the paragraph and put the table there."""
        paragraph = target.element
        if isinstance(blocks[0], TableBlock):
            delete_runs(paragraph, self._marks)
            mark_paragraph_deleted(paragraph, self._marks)
            self._insert_after(paragraph, self._build(blocks, paragraph, template, edit_id))
            return
        insert_point = delete_runs(paragraph, self._marks)
        append_inserted_runs(paragraph, blocks[0].segments, template.run, self._marks, after=insert_point)
        self._register_inserted([paragraph], edit_id, in_table=target.in_table)
        self._insert_after(paragraph, self._build(blocks[1:], paragraph, template, edit_id))

    def _replace_inserted_text(self, target: IndexedParagraph, blocks: list[Block], template: _Template, edit_id: str) -> None:
        """Replace text an earlier edit of this run inserted: its w:ins runs go, the new runs take their place.

        The earlier insertion has the same author and run, so it is removed rather than tracked
        as deleted (Word does the same when an author deletes their own insertion). Runs of the
        original document stay as they are, already tracked as deleted.
        """
        paragraph = target.element
        for child in list(paragraph):
            if child.tag == qn("ins"):
                self._marks.discard(child)
                paragraph.remove(child)
        deleted_content = [child for child in paragraph if child.tag in (qn("del"), qn("hyperlink"))]
        insert_point = deleted_content[-1] if deleted_content else None
        append_inserted_runs(paragraph, blocks[0].segments, template.run, self._marks, after=insert_point)
        self._register_inserted([paragraph], edit_id, in_table=target.in_table)
        self._insert_after(paragraph, self._build(blocks[1:], paragraph, template, edit_id))

    def _locate(self, edit: Edit, outcome: EditOutcome) -> IndexedParagraph | None:
        """Match the anchor in the original paragraphs, then in inserted ones when none matched."""
        try:
            target, inserted_by = self._find(edit.anchor, edit.occurrence)
        except AnchorError as error:
            outcome.problems.append(Problem("match", error.reason, tuple(error.candidates)))
            return None
        outcome.matched = target
        outcome.matched_inserted_by = inserted_by
        return target

    def _find(self, text: str, occurrence: int | None) -> tuple[IndexedParagraph, str | None]:
        """The one paragraph text selects by the anchor rules, and the edit that inserted it (None: an original one).

        The original paragraphs are searched first; paragraphs inserted by earlier edits only when
        none of them matches. Raises AnchorError when neither search selects exactly one: the
        original no-match error, or the inserted search's error when it found several matches.
        """
        try:
            return match_anchor(text, self._original, occurrence), None
        except AnchorError as error:
            if error.kind != "no_match" or not self._inserted:
                raise
            original_error = error
        try:
            target = match_anchor(text, self._inserted, occurrence)
        except AnchorError as inserted_error:
            if inserted_error.kind == "no_match":
                raise original_error from None
            raise
        return target, self._inserted_by[id(target.element)]

    def _list_plan(self, edit: Edit, outcome: EditOutcome) -> _ListPlan | None:
        """Resolve the Numbering key to the named list item's numId and level; None without the key or on a problem."""
        numbering = edit.numbering
        if numbering is None:
            return None
        if numbering.mode == NUMBERING_NONE:
            return _ListPlan(NUMBERING_NONE)
        label = f"Numbering prefix '{numbering.prefix}'"
        try:
            named, _ = self._find(numbering.prefix, None)
        except AnchorError as error:
            outcome.problems.append(_prefix_problem(label, error))
            return None
        num_id = paragraph_num_id(named.element)
        if num_id is None:
            outcome.problems.append(Problem("numbering", f"{label} names a paragraph that is not a numbered list item"))
            return None
        if numbering.mode == NUMBERING_NEW and not self._has_num(num_id):
            outcome.problems.append(Problem("numbering", f"{label}: numId {num_id} has no w:num in {NUMBERING_PART}"))
            return None
        return _ListPlan(numbering.mode, num_id, paragraph_level(named.element))

    def _plan_restyle(
        self, edit: Edit, paragraph: etree._Element, list_plan: _ListPlan | None, outcome: EditOutcome
    ) -> _Restyle | None:
        """What a replace changes in its paragraph's w:pPr; None for other operations and when nothing changes.

        A Style other than the paragraph's own is set. A Style that is not a list alias also takes
        the paragraph out of its list, unless a Numbering key decides the list. An unknown Style is
        a warning and changes nothing. A change the paragraph cannot record (it already holds a
        w:pPrChange) is a conflict.
        """
        if edit.operation != "replace":
            return None
        style_id = None
        numbering = list_plan
        resolved = self._resolve_style(edit, outcome, STYLE_IGNORED)
        if resolved is not None and resolved.style_id != self._paragraph_style(paragraph):
            style_id = resolved.style_id
        if resolved is not None and not resolved.list_item and edit.numbering is None:
            numbering = _ListPlan(NUMBERING_NONE)
        if not _changes_numbering(paragraph, numbering):
            numbering = None
        if style_id is None and numbering is None:
            return None
        if not can_track_paragraph_change(paragraph, self._marks.author):
            outcome.problems.append(Problem("conflict", PROPERTY_CHANGE_CONFLICT))
        return _Restyle(style_id, numbering)

    def _restyle(self, paragraph: etree._Element, restyle: _Restyle, outcome: EditOutcome) -> None:
        """Give the paragraph the planned style and list numbering, recording its previous w:pPr unless this author inserted it."""
        old_style, old_num_id = self._paragraph_style(paragraph), paragraph_num_id(paragraph)
        properties = open_paragraph_change(paragraph, self._marks)
        if restyle.style_id is not None:
            set_paragraph_property(properties, "pStyle", make_element("pStyle", {"val": restyle.style_id}))
        if restyle.numbering is not None:
            set_paragraph_property(properties, "numPr", self._list_numbering(restyle.numbering))
        new_style, new_num_id = self._paragraph_style(paragraph), paragraph_num_id(paragraph)
        outcome.property_change = PropertyChange(old_style, old_num_id, new_style, new_num_id)

    def _template(self, edit: Edit, paragraph: etree._Element, list_plan: _ListPlan | None, outcome: EditOutcome) -> _Template:
        """What new paragraphs start from: the paragraph's own properties (replace, delete), else the insert template with the Numbering key's list."""
        if edit.operation in CHANGING_OPERATIONS:
            return _Template(copied_paragraph_properties(paragraph), first_text_run_properties(paragraph))
        template = self._insert_template(edit, paragraph, outcome)
        if list_plan is None:
            return template
        return template.with_numbering(self._list_numbering(list_plan))

    def _list_numbering(self, plan: _ListPlan) -> etree._Element | None:
        """The w:numPr a resolved Numbering key gives (None for none); for new, its w:num is added to the numbering part first."""
        if plan.mode == NUMBERING_NONE:
            return None
        num_id = plan.num_id
        if plan.mode == NUMBERING_NEW:
            numbering = self._numbering_part()
            num_id = add_restarted_num(numbering, plan.num_id, [plan.level])
            self._package.set_xml_part(NUMBERING_PART, numbering)
        return numbering_properties(num_id, plan.level)

    def _has_num(self, num_id: str) -> bool:
        """True when word/numbering.xml defines a w:num with this id."""
        numbering = self._numbering_part()
        return numbering is not None and find_num(numbering, num_id) is not None

    def _insert_template(self, edit: Edit, anchor: etree._Element, outcome: EditOutcome) -> _Template:
        """Properties for inserted paragraphs: the anchor's, or the resolved Style's.

        Without Style, or when Style resolves to the anchor's own style, the anchor's paragraph
        and run properties are copied (list numbering only when the Style is a list alias). A
        different style gives a bare w:pStyle. A style neither defined nor aliased falls back to
        the anchor's properties with a warning. A list alias that finds no numbering to keep is a
        warning unless a Numbering key decides the list.
        """
        from_anchor = _Template(copied_paragraph_properties(anchor), first_text_run_properties(anchor))
        resolved = self._resolve_style(edit, outcome, "copied the anchor paragraph's properties instead")
        if resolved is None:
            return from_anchor
        if resolved.layout == "figure":
            return _Template(figure_paragraph_properties(resolved.style_id), None)
        if resolved.style_id == self._paragraph_style(anchor):
            template = from_anchor
            if not resolved.list_item and template.paragraph is not None:
                remove_children(template.paragraph, ("numPr",))
        else:
            template = _Template(styled_paragraph_properties(resolved.style_id), None)
        has_numbering = template.paragraph is not None and template.paragraph.find(qn("numPr")) is not None
        if resolved.list_item and not has_numbering and edit.numbering is None:
            outcome.warnings.append(
                f"Style '{edit.style}' asks for a list item, but the anchor paragraph is not one; inserted without list "
                "numbering (a Numbering key names the list)"
            )
        return template

    def _warn_about_style_on_delete(self, edit: Edit, paragraph: etree._Element, outcome: EditOutcome) -> None:
        """Style on delete never changes w:pPr; a different or unknown style is a warning."""
        resolved = self._resolve_style(edit, outcome, STYLE_IGNORED)
        if resolved is None:
            return
        current = self._paragraph_style(paragraph)
        if resolved.style_id != current:
            outcome.warnings.append(
                f"Style '{edit.style}' resolves to style id '{resolved.style_id}', but the paragraph has "
                f"'{current}'; paragraph properties kept"
            )

    def _resolve_style(self, edit: Edit, outcome: EditOutcome, fallback: str) -> ResolvedStyle | None:
        """The edit's Style resolved; None without one, or when it is unknown (a warning that names the fallback)."""
        if edit.style is None:
            return None
        try:
            return self._style_catalog().resolve(edit.style)
        except StyleNotFound as error:
            outcome.warnings.append(f"{error}; {fallback}")
            return None

    def _text_blocks(self, edit: Edit, outcome: EditOutcome) -> list[Block]:
        """Parse Text into blocks and check that any table can be built."""
        if edit.operation == "delete":
            return []
        try:
            blocks = parse_text_blocks(edit.text_lines)
        except MarkupError as error:
            outcome.problems.append(Problem("markup", str(error)))
            return []
        if any(isinstance(block, TableBlock) for block in blocks) and not self._tables:
            outcome.problems.append(Problem("table", NO_TABLE_MESSAGE))
        return blocks

    def _check_conflicts(self, edit: Edit, element: etree._Element, outcome: EditOutcome) -> None:
        """Refuse a second replace or delete of one paragraph, row or table, and changes to inserted ones."""
        if edit.operation not in CHANGING_OPERATIONS:
            return
        if outcome.matched_inserted_by is not None and edit.operation == "delete":
            message = f"delete of text inserted by {outcome.matched_inserted_by} is not supported"
            outcome.problems.append(Problem("conflict", message))
            return
        if outcome.matched_inserted_by is not None:
            return
        earlier = self._changed_by.get(id(element))
        if earlier is not None:
            outcome.problems.append(Problem("conflict", f"already changed by {earlier}"))

    # ROW: and TABLE: anchors

    def _apply_table_edit(self, edit: Edit, outcome: EditOutcome) -> None:
        """Check and run an edit anchored on a table row or a whole table."""
        is_row = edit.anchor.startswith(ROW_ANCHOR)
        value = edit.anchor.split(":", 1)[1].strip()
        if edit.style is not None:
            outcome.warnings.append("Style is not used for ROW: and TABLE: edits")
        target = self._locate_table_target(edit, is_row, value, outcome)
        rows, table_block = self._table_text(edit, is_row, outcome)
        if target is not None and rows:
            self._check_row_shape(target, rows, outcome)
        if target is not None:
            self._check_conflicts(edit, target.element, outcome)
            if is_row:
                self._check_conflicts(edit, target.table.element, outcome)
        if outcome.problems:
            return
        if is_row:
            self._run_row_edit(edit, target, rows)
        else:
            self._run_table_edit(edit, target, table_block)
        if edit.operation in CHANGING_OPERATIONS:
            self._changed_by[id(target.element)] = edit.edit_id

    def _locate_table_target(self, edit: Edit, is_row: bool, value: str, outcome: EditOutcome) -> IndexedRow | IndexedTable | None:
        """Match a ROW: or TABLE: anchor; record a match problem when it does not pick exactly one."""
        try:
            if is_row:
                target = match_row(value, self._tables, edit.table, edit.occurrence)
                outcome.matched = target.lead
            else:
                target = match_table(value, self._tables, edit.occurrence)
                outcome.matched = target.rows[0].lead
            return target
        except AnchorError as error:
            outcome.problems.append(Problem("match", error.reason, tuple(error.candidates)))
            return None

    def _table_text(self, edit: Edit, is_row: bool, outcome: EditOutcome) -> tuple[list[Row], TableBlock | None]:
        """Parse Text as rows (ROW: edits) or as one Markdown table (TABLE: replace)."""
        if edit.operation == "delete":
            return [], None
        if not is_row and edit.operation in INSERT_OPERATIONS:
            message = "insert_after and insert_before are not defined for TABLE: anchors; anchor on a paragraph or a ROW: instead"
            outcome.problems.append(Problem("rule", message))
            return [], None
        try:
            if is_row:
                rows = parse_row_lines(edit.text_lines)
                if edit.operation == "replace" and len(rows) != 1:
                    outcome.problems.append(Problem("markup", f"a ROW: replace takes exactly one table row, got {len(rows)}"))
                return rows, None
            blocks = parse_text_blocks(edit.text_lines)
        except MarkupError as error:
            outcome.problems.append(Problem("markup", str(error)))
            return [], None
        if len(blocks) != 1 or not isinstance(blocks[0], TableBlock):
            outcome.problems.append(Problem("markup", "a TABLE: replace takes exactly one Markdown table as Text"))
            return [], None
        return [], blocks[0]

    def _check_row_shape(self, target: IndexedRow, rows: list[Row], outcome: EditOutcome) -> None:
        """Every Text row must have as many cells as the anchored row."""
        cell_count = len(target.element.findall(qn("tc")))
        for row in rows:
            if len(row) != cell_count:
                outcome.problems.append(Problem("table", f"the row has {cell_count} cells, the Text row has {len(row)}"))
                return

    def _run_row_edit(self, edit: Edit, target: IndexedRow, rows: list[Row]) -> None:
        """Delete, replace or insert rows next to the anchored row."""
        row = target.element
        if edit.operation == "delete":
            delete_row(row, self._marks)
        elif edit.operation == "replace":
            replace_row_cells(row, rows[0], self._marks)
        else:
            new_rows = [build_inserted_row(row, cells, self._marks) for cells in rows]
            for new_row in new_rows:
                self._register_inserted(list(new_row.iter(qn("p"))), edit.edit_id, in_table=True)
            if edit.operation == "insert_after":
                self._insert_after(row, new_rows)
            else:
                for new_row in new_rows:
                    row.addprevious(new_row)

    def _run_table_edit(self, edit: Edit, target: IndexedTable, block: TableBlock | None) -> None:
        """Delete a whole table, or delete it and place the new table right after it."""
        delete_table(target.element, self._marks)
        if edit.operation == "replace":
            new_table = self._build_table(block, target.element, target.element)
            self._register_inserted(list(new_table.iter(qn("p"))), edit.edit_id, in_table=True)
            self._insert_after(target.element, [new_table])

    # Shared helpers

    def _insert_after(self, anchor: etree._Element, elements: list[etree._Element]) -> None:
        """Place elements after the anchor, after anything earlier edits placed there."""
        cursor = self._last_after.get(id(anchor), anchor)
        for element in elements:
            cursor.addnext(element)
            cursor = element
        self._last_after[id(anchor)] = cursor

    def _build(self, blocks: list[Block], anchor: etree._Element, template: _Template, edit_id: str) -> list[etree._Element]:
        """Build the tracked paragraphs and tables for blocks and index their paragraphs."""
        elements = []
        for block in blocks:
            if isinstance(block, TableBlock):
                element = self._build_table(block, self._reference_table(anchor), anchor)
                self._register_inserted(list(element.iter(qn("p"))), edit_id, in_table=True)
            else:
                element = build_inserted_paragraph(template.paragraph_copy(), block.segments, template.run, self._marks)
                self._register_inserted([element], edit_id, in_table=False)
            elements.append(element)
        return elements

    def _build_table(self, block: TableBlock, reference: etree._Element, placed_at: etree._Element) -> etree._Element:
        """Build a tracked table sized for the section of placed_at; review notes and a font warning go to the edit."""
        table = build_inserted_table(block, reference, self._marks, column_width_or_none(placed_at), self._table_notes)
        font = body_font_name(self._package.read_part(STYLES_PART))
        if font is not None and font != ESTIMATE_FONT:
            self._table_notes.append(f"table widths are estimated with {ESTIMATE_FONT} metrics, but the body font is {font}")
        return table

    def _register_inserted(self, paragraphs: list[etree._Element], edit_id: str, in_table: bool) -> None:
        """Make inserted text searchable for later anchors; a paragraph registered again gets its new text."""
        registered = {id(paragraph) for paragraph in paragraphs}
        self._inserted = [item for item in self._inserted if id(item.element) not in registered]
        for paragraph in paragraphs:
            self._inserted.append(IndexedParagraph(paragraph, paragraph_text(paragraph), 0, in_table))
            self._inserted_by[id(paragraph)] = edit_id

    def _reference_table(self, anchor: etree._Element) -> etree._Element:
        """The original table nearest before the anchor in document order, else the first one."""
        return nearest_table_before(self._body, anchor, lambda table: id(table) in self._original_table_ids)

    def _paragraph_style(self, paragraph: etree._Element) -> str | None:
        """The paragraph's style id: its w:pStyle, or the default paragraph style."""
        style = paragraph.find(f"{qn('pPr')}/{qn('pStyle')}")
        if style is not None:
            return style.get(qn("val"))
        return self._style_catalog().default_style_id()

    def _style_catalog(self) -> StyleCatalog:
        """Read styles.xml and the alias map the first time a Style is needed."""
        if self._styles is None:
            self._styles = StyleCatalog(self._package.read_part(STYLES_PART), load_aliases())
        return self._styles

    def _numbering_part(self) -> etree._Element | None:
        """word/numbering.xml, read the first time a Numbering key needs it; None when the package has none."""
        if self._numbering is None and self._package.has_part(NUMBERING_PART):
            self._numbering = self._package.read_part(NUMBERING_PART)
        return self._numbering


def _changes_numbering(paragraph: etree._Element, plan: _ListPlan | None) -> bool:
    """True when giving the paragraph the planned list numbering changes its w:numPr (no plan changes nothing)."""
    if plan is None:
        return False
    if plan.mode == NUMBERING_NEW:
        return True
    current_num_id = paragraph_num_id(paragraph)
    if plan.mode == NUMBERING_NONE:
        return current_num_id is not None
    return (current_num_id, paragraph_level(paragraph)) != (plan.num_id, plan.level)


def _prefix_problem(label: str, error: AnchorError) -> Problem:
    """A match problem for a Numbering prefix; Numbering takes no Occurrence, so an ambiguous prefix is to be lengthened."""
    reason = error.reason
    if error.kind == "ambiguous":
        reason = f"the prefix matches {len(error.candidates)} paragraphs; lengthen it to pick one"
    return Problem("match", f"{label}: {reason}", tuple(error.candidates))
