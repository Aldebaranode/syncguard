"""Build the folder that fill reads from the harness results, as docs/thesis/fill-layout.json says.

The harness writes values.json as {"values", "extras", "roles", "uncomputed"}, one tabel-*.md per
table (a title line above a Markdown table) and gambar-*.png figures. fill reads one flat
values.json, one <token>.md per {{TABLE:token}} paragraph and one <token>.png per {{FIGURE:token}}
paragraph. prep flattens the values (the harness values, its extras and the layout's
recorded_values; a name given twice is a problem), shapes each table as its layout entry says,
decides each table's orientation with fill's own floor (10 pt with 60 twip cell margins) against
the layout's portrait and landscape text widths, lists the captions of the landscape tables for the
landscape pass, and copies the figures. Every problem goes to the report; nothing is guessed.
"""

import json
import re
import shlex
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from redline.edits import parse_edits
from redline.fill import TOKEN, VALUES_FILE, FillError, load_table_file, load_values
from redline.markup import MarkupError, format_table_row, table_cells
from redline.tables import needed_width

HARNESS_VALUES = "values.json"
LANDSCAPE_CAPTIONS = "landscape-captions.txt"
LANDSCAPE_COMMAND = "python3 -m redline landscape --docx IN --out OUT --report REPORT --author NAME --at"
PORTRAIT = "portrait"
LANDSCAPE = "landscape"
TOO_WIDE = "too wide for landscape"
FIGURE_PATH = "source"
FIGURE_VALUE = "source_value"
SESSION_COLUMN = "Sesi"
EMPTY_CELLS = ("", "-")
TEMPLATE_FIELD = re.compile(r"\{([^{}]+)\}")
TWIPS_PER_CM = 1440 / 2.54
NO_HARNESS_VALUE = "no value in the harness output"


class PrepError(ValueError):
    """An input prep cannot work with at all: the layout, the harness values.json, or a used --out folder."""


class _TableProblem(ValueError):
    """Why one table cannot be written; the message goes to the report's problems."""


@dataclass(frozen=True)
class Transpose:
    """Rows turned into columns: the first column is headed label, each source row becomes a column headed by heading."""

    label: str
    heading: str


@dataclass(frozen=True)
class TableLayout:
    """One table of the layout: its token, harness file and caption, and how to shape it."""

    token: str
    source: str
    caption: str
    manual: str | None
    columns: tuple[str, ...] | None
    drop: tuple[str, ...]
    drop_empty: bool
    drop_single_session: bool
    transpose: Transpose | None


@dataclass(frozen=True)
class FigureLayout:
    """One figure: its token, and its file as a path below the root (origin source) or as the value naming a results file (origin source_value)."""

    token: str
    origin: str
    reference: str


@dataclass(frozen=True)
class Layout:
    """The checked contents of the layout file."""

    results_dir: str
    portrait_twips: int
    landscape_twips: int
    recorded_values: dict[str, object]
    tables: tuple[TableLayout, ...]
    figures: tuple[FigureLayout, ...]


@dataclass(frozen=True)
class HarnessValues:
    """The parts of the harness values.json that prep uses."""

    values: dict[str, object]
    extras: dict[str, object]
    uncomputed: dict[str, str]


@dataclass(frozen=True)
class DroppedColumn:
    """A source column left out of the written table, and why."""

    name: str
    reason: str


@dataclass(frozen=True)
class WrittenTable:
    """A table written to the fill folder: its source, shape and columns, its width at the floor in twips, and its orientation."""

    token: str
    source: str
    caption: str
    rows: int
    source_rows: int
    kept: tuple[str, ...]
    dropped: tuple[DroppedColumn, ...]
    transposed: bool
    needed_twips: int
    orientation: str


@dataclass(frozen=True)
class ManualTable:
    """A table the author builds by hand, with the layout's note; prep writes nothing for it."""

    token: str
    caption: str
    note: str


@dataclass(frozen=True)
class CopiedFigure:
    """A figure copied to <token>.png, with where its file came from."""

    token: str
    source: str


@dataclass(frozen=True)
class MissingToken:
    """A {{DATA:name}} of the drafts without a value: the drafts it is in, whether an edit's Text uses it, and why it has none."""

    name: str
    drafts: tuple[str, ...]
    in_edit_text: bool
    reason: str


@dataclass
class PrepResult:
    """What prep wrote and found, for the report; ok when no problem was found."""

    results: str
    drafts: tuple[str, ...]
    values_written: int = 0
    uncomputed: dict[str, str] = field(default_factory=dict)
    missing_tokens: list[MissingToken] = field(default_factory=list)
    tables: list[WrittenTable] = field(default_factory=list)
    manual: list[ManualTable] = field(default_factory=list)
    figures: list[CopiedFigure] = field(default_factory=list)
    landscape_captions: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when no problem was found."""
        return not self.problems


def prepare(layout_path: Path, root: Path, out: Path, results: Path | None = None, drafts: Sequence[Path] = ()) -> PrepResult:
    """Build the fill folder at out from the harness results; one entry point for the flow.

    results overrides the layout's results_dir (a folder of scratch output for a trial run).
    Everything that can stop the run (a used --out, the layout, the harness values.json and the
    drafts) is checked or read before the first file is written, so a stopped run leaves no files.
    """
    out = Path(out)
    _refuse_used_folder(out)
    layout = load_layout(Path(layout_path))
    results_dir = Path(results) if results is not None else Path(root) / layout.results_dir
    harness = load_harness_values(results_dir / HARNESS_VALUES)
    result = PrepResult(str(results_dir), tuple(Path(path).name for path in drafts), uncomputed=harness.uncomputed)
    values = flatten_values(harness, layout.recorded_values, result.problems)
    if drafts:
        result.missing_tokens = missing_draft_tokens(drafts, values, harness.uncomputed)
    out.mkdir(parents=True, exist_ok=True)
    _write_values(values, out, result)
    for entry in layout.tables:
        _prepare_table(entry, layout, results_dir, out, result)
    result.landscape_captions = [table.caption for table in result.tables if table.orientation == LANDSCAPE]
    (out / LANDSCAPE_CAPTIONS).write_text("".join(f"{caption}\n" for caption in result.landscape_captions), encoding="utf-8")
    for figure in layout.figures:
        _copy_figure(figure, Path(root), results_dir, values, out, result)
    return result


def landscape_command(captions: Sequence[str]) -> str:
    """The landscape pass for these captions, each quoted for the shell; IN, OUT, REPORT and NAME are to be filled in."""
    return " ".join([LANDSCAPE_COMMAND, *(shlex.quote(caption) for caption in captions)])


def centimetres(twips: int) -> str:
    """A width in twips as centimetres with one decimal."""
    return f"{twips / TWIPS_PER_CM:.1f}"


def flatten_values(harness: HarnessValues, recorded: dict[str, object], problems: list[str]) -> dict[str, object]:
    """The harness values, then its extras, then the layout's recorded values in one object.

    A name given twice keeps its first value and is a problem.
    """
    flat: dict[str, object] = {}
    given_by: dict[str, str] = {}
    sources = (("the harness values", harness.values), ("the harness extras", harness.extras), ("recorded_values", recorded))
    for origin, values in sources:
        for name, value in values.items():
            if name in flat:
                problems.append(f"value {name}: given by {given_by[name]} and by {origin}; the first is kept")
                continue
            flat[name] = value
            given_by[name] = origin
    return flat


def missing_draft_tokens(drafts: Sequence[Path], values: dict[str, object], uncomputed: dict[str, str]) -> list[MissingToken]:
    """Every {{DATA:name}} of the drafts without a value, whether an edit's Text or only a notes section uses it."""
    drafts_of: dict[str, list[str]] = {}
    in_edit_text: set[str] = set()
    for path in drafts:
        markdown = Path(path).read_text(encoding="utf-8")
        for name in TOKEN.findall(markdown):
            names = drafts_of.setdefault(name, [])
            if Path(path).name not in names:
                names.append(Path(path).name)
        for edit in parse_edits(markdown, str(path)).edits:
            for line in edit.text_lines:
                in_edit_text.update(TOKEN.findall(line))
    missing = []
    for name in sorted(drafts_of):
        if name in values:
            continue
        missing.append(MissingToken(name, tuple(drafts_of[name]), name in in_edit_text, uncomputed.get(name, NO_HARNESS_VALUE)))
    return missing


def load_layout(path: Path) -> Layout:
    """Read and check the layout file; anything malformed raises PrepError naming where it is."""
    where = str(path)
    data = _json_object(path)
    widths = _object(data, "text_width_twips", where)
    tables = tuple(_table_layout(entry, number) for number, entry in enumerate(_items(data, "tables", where), start=1))
    figures = tuple(_figure_layout(entry, number) for number, entry in enumerate(_items(data, "figures", where), start=1))
    return Layout(
        results_dir=_text(data, "results_dir", where),
        portrait_twips=_twips(widths, "portrait", "text_width_twips"),
        landscape_twips=_twips(widths, "landscape", "text_width_twips"),
        recorded_values=_object(data, "recorded_values", where, required=False),
        tables=tables,
        figures=figures,
    )


def load_harness_values(path: Path) -> HarnessValues:
    """Read the harness values.json: values is required, extras and uncomputed may be left out."""
    if not Path(path).is_file():
        raise PrepError(f"{path} does not exist; the results folder must hold the harness values.json")
    where = str(path)
    data = _json_object(path)
    uncomputed = _object(data, "uncomputed", where, required=False)
    return HarnessValues(
        values=_object(data, "values", where),
        extras=_object(data, "extras", where, required=False),
        uncomputed={name: str(reason) for name, reason in uncomputed.items()},
    )


def _refuse_used_folder(out: Path) -> None:
    """--out must not exist yet or be an empty folder, so that nothing of an earlier run is mixed in."""
    if not out.exists():
        return
    if not out.is_dir():
        raise PrepError(f"{out} is not a folder")
    if any(out.iterdir()):
        raise PrepError(f"{out} is not empty; choose a new --out")


def _write_values(values: dict[str, object], out: Path, result: PrepResult) -> None:
    """Write the flattened values as out/values.json and check that fill's own reader accepts it."""
    path = out / VALUES_FILE
    path.write_text(json.dumps(values, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result.values_written = len(values)
    try:
        load_values(path)
    except FillError as error:
        result.problems.append(str(error))


def _prepare_table(entry: TableLayout, layout: Layout, results: Path, out: Path, result: PrepResult) -> None:
    """Write one table of the layout, or record why it cannot be written; a manual table is only listed."""
    if entry.manual is not None:
        result.manual.append(ManualTable(entry.token, entry.caption, entry.manual))
        return
    try:
        written = _write_table(entry, layout, results, out)
    except _TableProblem as problem:
        result.problems.append(f"table {entry.token}: {problem}")
        return
    result.tables.append(written)
    if written.orientation == TOO_WIDE:
        result.problems.append(
            f"table {entry.token} ({entry.caption}) needs {written.needed_twips} twips ({centimetres(written.needed_twips)} cm) at 10 pt, "
            f"more than the landscape text width of {layout.landscape_twips} twips; revise its layout"
        )


def _write_table(entry: TableLayout, layout: Layout, results: Path, out: Path) -> WrittenTable:
    """Shape the source table as the entry says, write <token>.md, and measure it as fill will."""
    header, rows = _read_source_table(results, entry.source)
    kept, dropped = _kept_columns(entry, header, rows)
    if entry.transpose is None:
        indexes = [header.index(name) for name in kept]
        table = [kept] + [[row[index] for index in indexes] for row in rows]
    else:
        table = _transposed(entry.transpose, entry.source, header, rows, kept)
    path = out / f"{entry.token}.md"
    path.write_text(_markdown_table(table), encoding="utf-8")
    needed = needed_width(load_table_file(path))
    return WrittenTable(
        token=entry.token,
        source=entry.source,
        caption=entry.caption,
        rows=len(table) - 1,
        source_rows=len(rows),
        kept=tuple(kept),
        dropped=tuple(dropped),
        transposed=entry.transpose is not None,
        needed_twips=needed,
        orientation=_orientation(needed, layout),
    )


def _read_source_table(results: Path, source: str) -> tuple[list[str], list[list[str]]]:
    """The header and body rows of the first Markdown table in a harness file; the lines before it are its title."""
    path = results / source
    if not path.is_file():
        raise _TableProblem(f"{source} is not in the results folder")
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    start = next((index for index, line in enumerate(lines) if line.startswith("|")), None)
    if start is None:
        raise _TableProblem(f"{source} holds no Markdown table")
    end = start
    while end < len(lines) and lines[end].startswith("|"):
        end += 1
    try:
        header, *rows = table_cells(lines[start:end])
    except MarkupError as error:
        raise _TableProblem(f"{source}: {error}") from error
    if not rows:
        raise _TableProblem(f"{source} has no rows")
    return header, rows


def _kept_columns(entry: TableLayout, header: list[str], rows: list[list[str]]) -> tuple[list[str], list[DroppedColumn]]:
    """The columns to write, in order, and the source columns left out with the reason for each."""
    if entry.columns is not None:
        _check_known(entry.columns, header, "columns names", entry.source)
        dropped = [DroppedColumn(name, "not listed in columns") for name in header if name not in entry.columns]
        return list(entry.columns), dropped
    _check_known(entry.drop, header, "drop names", entry.source)
    kept: list[str] = []
    dropped = []
    for index, name in enumerate(header):
        reason = _drop_reason(entry, name, [row[index] for row in rows])
        if reason is None:
            kept.append(name)
        else:
            dropped.append(DroppedColumn(name, reason))
    if not kept:
        raise _TableProblem(f"no column of {entry.source} is left after the drop rules")
    return kept, dropped


def _drop_reason(entry: TableLayout, name: str, cells: list[str]) -> str | None:
    """Why the drop rules leave a column out, or None when it stays."""
    if name in entry.drop:
        return "listed in drop"
    if entry.drop_empty and all(cell.strip() in EMPTY_CELLS for cell in cells):
        return "empty or '-' on every row"
    if entry.drop_single_session and name == SESSION_COLUMN and len(set(cells)) == 1:
        return f"one session ({cells[0]}) on every row"
    return None


def _check_known(names: Sequence[str], header: list[str], what: str, source: str) -> None:
    """Raise a table problem naming every name that the source table does not have."""
    unknown = [name for name in names if name not in header]
    if unknown:
        raise _TableProblem(f"{what} {', '.join(unknown)}, which {source} does not have")


def _transposed(transpose: Transpose, source: str, header: list[str], rows: list[list[str]], kept: list[str]) -> list[list[str]]:
    """The rows turned into columns: label above the kept columns the heading does not use, then one column per source row."""
    used = TEMPLATE_FIELD.findall(transpose.heading)
    _check_known(used, header, "the transpose heading uses", source)
    listed = [name for name in kept if name not in used]
    if not listed:
        raise _TableProblem(f"the transpose heading uses every kept column of {source}, so no row is left")
    table = [[transpose.label] + [_heading(transpose.heading, header, row) for row in rows]]
    for name in listed:
        index = header.index(name)
        table.append([name] + [row[index] for row in rows])
    return table


def _heading(template: str, header: list[str], row: list[str]) -> str:
    """The heading template with every {Column} replaced by that column's cell in the row."""
    return TEMPLATE_FIELD.sub(lambda column: row[header.index(column.group(1))], template)


def _markdown_table(table: list[list[str]]) -> str:
    """The rows as one Markdown table: the header row, the separator row, then the body rows."""
    header, *body = table
    lines = [format_table_row(header), format_table_row(["---"] * len(header))]
    lines += [format_table_row(row) for row in body]
    return "\n".join(lines) + "\n"


def _orientation(needed: int, layout: Layout) -> str:
    """Portrait when the table fits the portrait text width at the floor, else landscape when it fits that, else too wide."""
    if needed <= layout.portrait_twips:
        return PORTRAIT
    if needed <= layout.landscape_twips:
        return LANDSCAPE
    return TOO_WIDE


def _copy_figure(figure: FigureLayout, root: Path, results: Path, values: dict[str, object], out: Path, result: PrepResult) -> None:
    """Copy one figure to out/<token>.png, or record why it cannot be copied."""
    if figure.origin == FIGURE_PATH:
        path = root / figure.reference
        source = figure.reference
    else:
        if figure.reference not in values:
            result.problems.append(f"figure {figure.token}: the value {figure.reference} is not among the values")
            return
        file_name = str(values[figure.reference]).strip()
        if not file_name:
            result.problems.append(f"figure {figure.token}: the value {figure.reference} is empty")
            return
        path = results / file_name
        source = f"{figure.reference} = {file_name}"
    if not path.is_file():
        result.problems.append(f"figure {figure.token}: {path} does not exist")
        return
    shutil.copyfile(path, out / f"{figure.token}.png")
    result.figures.append(CopiedFigure(figure.token, source))


def _table_layout(entry: object, number: int) -> TableLayout:
    """Check one entry of the layout's tables."""
    where = f"tables entry {number}"
    if not isinstance(entry, dict):
        raise PrepError(f"{where} must be an object")
    transpose = None
    if "transpose" in entry:
        settings = _object(entry, "transpose", where)
        transpose = Transpose(_text(settings, "label", f"{where} transpose"), _text(settings, "heading", f"{where} transpose"))
    return TableLayout(
        token=_text(entry, "token", where),
        source=_text(entry, "source", where),
        caption=_text(entry, "caption", where),
        manual=_optional_text(entry, "manual", where),
        columns=_names(entry, "columns", where),
        drop=_names(entry, "drop", where) or (),
        drop_empty=_flag(entry, "drop_empty", where),
        drop_single_session=_flag(entry, "drop_single_session", where),
        transpose=transpose,
    )


def _figure_layout(entry: object, number: int) -> FigureLayout:
    """Check one entry of the layout's figures: a token and exactly one of source and source_value."""
    where = f"figures entry {number}"
    if not isinstance(entry, dict):
        raise PrepError(f"{where} must be an object")
    origins = [key for key in (FIGURE_PATH, FIGURE_VALUE) if key in entry]
    if len(origins) != 1:
        raise PrepError(f"{where} must have exactly one of source and source_value")
    return FigureLayout(_text(entry, "token", where), origins[0], _text(entry, origins[0], where))


def _json_object(path: Path) -> dict:
    """Read a file that must hold one JSON object."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise PrepError(f"{path} is not valid JSON: {error}") from error
    if not isinstance(data, dict):
        raise PrepError(f"{path} must hold one JSON object")
    return data


def _object(data: dict, key: str, where: str, required: bool = True) -> dict:
    """An object member; an empty one when the member may be left out and is."""
    if key not in data and not required:
        return {}
    value = data.get(key)
    if not isinstance(value, dict):
        raise PrepError(f"{where}: {key} must be an object")
    return value


def _items(data: dict, key: str, where: str) -> list:
    """A list member; an empty list when it is left out."""
    value = data.get(key, [])
    if not isinstance(value, list):
        raise PrepError(f"{where}: {key} must be a list")
    return value


def _text(data: dict, key: str, where: str) -> str:
    """A required non-empty string member."""
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise PrepError(f"{where}: {key} must be a non-empty string")
    return value


def _optional_text(data: dict, key: str, where: str) -> str | None:
    """A string member that may be left out (None then)."""
    if key not in data:
        return None
    return _text(data, key, where)


def _names(data: dict, key: str, where: str) -> tuple[str, ...] | None:
    """A list of column names that may be left out (None then)."""
    if key not in data:
        return None
    value = data[key]
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        raise PrepError(f"{where}: {key} must be a list of column names")
    return tuple(value)


def _flag(data: dict, key: str, where: str) -> bool:
    """A true or false member that is true when left out."""
    value = data.get(key, True)
    if not isinstance(value, bool):
        raise PrepError(f"{where}: {key} must be true or false")
    return value


def _twips(data: dict, key: str, where: str) -> int:
    """A positive whole number of twips."""
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PrepError(f"{where}: {key} must be a positive whole number of twips")
    return value
