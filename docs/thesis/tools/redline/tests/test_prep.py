"""Tests for prep: building the folder fill reads from the harness results, as the layout file says."""

import json
from pathlib import Path

import pytest

from docx_builder import png_bytes
from redline.fill import load_table_file, load_values
from redline.markup import table_cells
from redline.prep import LANDSCAPE, LANDSCAPE_CAPTIONS, PORTRAIT, TOO_WIDE, PrepError, landscape_command, prepare

HARNESS = {
    "values": {"S4.n": "10", "S4.fig.file": "gambar-failover-S5-iterasi-1.png"},
    "extras": {"partition.outcomes": "S9 iterasi 1: selesai"},
    "roles": {"S4": "S5"},
    "uncomputed": {"soak.availability": "no session has soak.json"},
}
FAILOVER = (
    "Tabel 4.3 Failover per iterasi\n\n"
    "| Sesi | Iterasi | TTD (s) | Alasan tidak valid | Keterangan |\n"
    "|---|---|---|---|---|\n"
    "| S5 | 1 | 13,442 | - |  |\n"
    "| S5 | 2 | 14,016 | - |  |\n"
)
TWO_SESSIONS = (
    "Skenario partisi jaringan\n\n"
    "| Sesi | Iterasi | Hasil | TTD (s) |\n"
    "|---|---|---|---|\n"
    "| S8 | 1 | failback tidak selesai | 32,166 |\n"
    "| S9 | 1 | failback selesai | 33,010 |\n"
    "| S9 | 2 | failback selesai | 32,658 |\n"
)
SAFETY = (
    "Tabel 4.7 Bukti keamanan per sesi\n\n"
    "| Sesi | Sampel monitor | Suara 48DC2183 di dalam interval |\n"
    "|---|---|---|\n"
    "| S5 | 2.279 | 0 |\n"
    "| S7 | 785 | 0 |\n"
)
IDENTIFIER = "failover.state_sync_interval"


def _table(*rows: list[str]) -> str:
    """A harness table file with a title line; the first row is the header."""
    lines = ["Judul", "", "| " + " | ".join(rows[0]) + " |", "|" + "---|" * len(rows[0])]
    lines += ["| " + " | ".join(row) + " |" for row in rows[1:]]
    return "\n".join(lines) + "\n"


def _repository(tmp_path: Path, files: dict[str, str], harness: dict, layout: dict) -> tuple[Path, Path]:
    """A repository root whose experiments/results holds values.json and the files, plus docs/fill-layout.json; returns the layout path and the root."""
    root = tmp_path / "repo"
    results = root / "experiments" / "results"
    results.mkdir(parents=True)
    (results / "values.json").write_text(json.dumps(harness), encoding="utf-8")
    for name, text in files.items():
        (results / name).write_text(text, encoding="utf-8")
    layout_path = root / "docs" / "fill-layout.json"
    layout_path.parent.mkdir(parents=True)
    full_layout = {"about": "test layout", "results_dir": "experiments/results", "text_width_twips": {"portrait": 7940, "landscape": 12867}}
    full_layout |= {"recorded_values": {}, "tables": [], "figures": []} | layout
    layout_path.write_text(json.dumps(full_layout), encoding="utf-8")
    return layout_path, root


def _run(tmp_path: Path, files: dict[str, str] | None = None, harness: dict | None = None, drafts: tuple[Path, ...] = (), **layout):
    """Write a repository with these results and layout keys, run prepare into tmp_path/out, and return the result and out."""
    layout_path, root = _repository(tmp_path, files or {}, harness or HARNESS, layout)
    out = tmp_path / "out"
    return prepare(layout_path, root, out, drafts=list(drafts)), out


def _cells(path: Path) -> list[list[str]]:
    """The cells of a written table file, after checking that fill accepts it."""
    load_table_file(path)
    return table_cells([line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()])


def _entry(token: str, source: str, **keys) -> dict:
    """One table entry of the layout."""
    return {"token": token, "source": source, "caption": f"Tabel {token}"} | keys


def test_values_extras_and_recorded_values_become_one_flat_values_json(tmp_path):
    """fill reads one object of name to value: the harness values, its extras and the layout's recorded values."""
    result, out = _run(tmp_path, recorded_values={"grafana.capture": "9 Oktober 2026 pukul 14:26 WIB"})
    assert load_values(out / "values.json") == {
        "S4.n": "10",
        "S4.fig.file": "gambar-failover-S5-iterasi-1.png",
        "partition.outcomes": "S9 iterasi 1: selesai",
        "grafana.capture": "9 Oktober 2026 pukul 14:26 WIB",
    }
    assert result.values_written == 4 and result.problems == []
    assert result.uncomputed == {"soak.availability": "no session has soak.json"}


def test_a_value_given_twice_is_a_problem_and_keeps_its_first_value(tmp_path):
    """A name in both values and extras, or recorded in the layout and also given by the harness, is an error."""
    harness = {"values": {"S4.n": "10"}, "extras": {"S4.n": "11"}, "uncomputed": {}}
    result, out = _run(tmp_path, harness=harness, recorded_values={"S4.n": "12"})
    assert result.problems == [
        "value S4.n: given by the harness values and by the harness extras; the first is kept",
        "value S4.n: given by the harness values and by recorded_values; the first is kept",
    ]
    assert load_values(out / "values.json") == {"S4.n": "10"}


def test_a_value_fill_cannot_read_is_a_problem(tmp_path):
    """fill takes strings and numbers only, so prep checks the written values.json with fill's own reader."""
    harness = {"values": {"S4.dual.iterations": None}, "uncomputed": {}}
    result, _ = _run(tmp_path, harness=harness)
    assert result.problems == ["value of 'S4.dual.iterations' must be a string or a number"]


def test_explicit_columns_are_written_exactly_in_their_order(tmp_path):
    """columns lists the output columns in order; the other source columns are dropped as not listed."""
    tables = [_entry("failover_iterations", "tabel-failover.md", columns=["Iterasi", "TTD (s)", "Sesi"])]
    result, out = _run(tmp_path, files={"tabel-failover.md": FAILOVER}, tables=tables)
    assert _cells(out / "failover_iterations.md") == [["Iterasi", "TTD (s)", "Sesi"], ["1", "13,442", "S5"], ["2", "14,016", "S5"]]
    [written] = result.tables
    assert (written.token, written.source, written.rows, written.kept) == ("failover_iterations", "tabel-failover.md", 2, ("Iterasi", "TTD (s)", "Sesi"))
    assert [(column.name, column.reason) for column in written.dropped] == [
        ("Alasan tidak valid", "not listed in columns"),
        ("Keterangan", "not listed in columns"),
    ]


def test_an_unknown_explicit_column_is_a_problem_and_writes_nothing(tmp_path):
    """A column the source table does not have sends the layout back; no partial table is written."""
    tables = [_entry("failover_iterations", "tabel-failover.md", columns=["Iterasi", "Tinggi"])]
    result, out = _run(tmp_path, files={"tabel-failover.md": FAILOVER}, tables=tables)
    assert result.problems == ["table failover_iterations: columns names Tinggi, which tabel-failover.md does not have"]
    assert result.tables == [] and not (out / "failover_iterations.md").exists()


def test_columns_listed_in_drop_or_empty_on_every_row_are_dropped_with_their_reason(tmp_path):
    """Without columns: drop, empty or '-' cells on every row (drop_empty) and a one-session Sesi column go; drop_empty can be switched off."""
    tables = [
        _entry("dropped", "tabel-failover.md", drop=["TTD (s)"]),
        _entry("kept_empty", "tabel-failover.md", drop_empty=False),
    ]
    result, out = _run(tmp_path, files={"tabel-failover.md": FAILOVER}, tables=tables)
    dropped, kept_empty = result.tables
    assert _cells(out / "dropped.md") == [["Iterasi"], ["1"], ["2"]]
    assert [(column.name, column.reason) for column in dropped.dropped] == [
        ("Sesi", "one session (S5) on every row"),
        ("TTD (s)", "listed in drop"),
        ("Alasan tidak valid", "empty or '-' on every row"),
        ("Keterangan", "empty or '-' on every row"),
    ]
    assert kept_empty.kept == ("Iterasi", "TTD (s)", "Alasan tidak valid", "Keterangan")


def test_an_unknown_drop_name_is_a_problem(tmp_path):
    """A drop name the source does not have is a typo in the layout, so nothing is written for that table."""
    result, out = _run(tmp_path, files={"tabel-failover.md": FAILOVER}, tables=[_entry("t", "tabel-failover.md", drop=["TTR (s)"])])
    assert result.problems == ["table t: drop names TTR (s), which tabel-failover.md does not have"]
    assert not (out / "t.md").exists()


def test_a_session_column_is_dropped_only_when_every_row_holds_one_session(tmp_path):
    """One distinct Sesi value is dropped, two are kept, and drop_single_session false keeps even one."""
    tables = [
        _entry("one", "tabel-failover.md"),
        _entry("two", "tabel-partisi.md"),
        _entry("kept", "tabel-failover.md", drop_single_session=False),
    ]
    result, _ = _run(tmp_path, files={"tabel-failover.md": FAILOVER, "tabel-partisi.md": TWO_SESSIONS}, tables=tables)
    one, two, kept = result.tables
    assert "Sesi" not in one.kept and ("Sesi", "one session (S5) on every row") in [(column.name, column.reason) for column in one.dropped]
    assert two.kept == ("Sesi", "Iterasi", "Hasil", "TTD (s)")
    assert kept.kept == ("Sesi", "Iterasi", "TTD (s)")


def test_transpose_with_a_two_column_heading_turns_each_iteration_into_a_column(tmp_path):
    """Tabel 4.10: the first column is headed by label and lists the kept columns the heading does not use."""
    transpose = {"label": "Butir", "heading": "{Sesi} iterasi {Iterasi}"}
    result, out = _run(tmp_path, files={"tabel-partisi.md": TWO_SESSIONS}, tables=[_entry("partition", "tabel-partisi.md", transpose=transpose)])
    assert _cells(out / "partition.md") == [
        ["Butir", "S8 iterasi 1", "S9 iterasi 1", "S9 iterasi 2"],
        ["Hasil", "failback tidak selesai", "failback selesai", "failback selesai"],
        ["TTD (s)", "32,166", "33,010", "32,658"],
    ]
    [written] = result.tables
    assert written.transposed is True and written.rows == 2 and written.source_rows == 3


def test_transpose_with_a_one_column_heading_turns_each_session_into_a_column(tmp_path):
    """Tabel 4.13: one column per session, one row per kept item."""
    transpose = {"label": "Butir", "heading": "{Sesi}"}
    result, out = _run(tmp_path, files={"tabel-4-7-keamanan.md": SAFETY}, tables=[_entry("safety_evidence", "tabel-4-7-keamanan.md", transpose=transpose)])
    assert _cells(out / "safety_evidence.md") == [
        ["Butir", "S5", "S7"],
        ["Sampel monitor", "2.279", "785"],
        ["Suara 48DC2183 di dalam interval", "0", "0"],
    ]
    assert result.problems == []


def test_a_transpose_heading_that_names_an_unknown_column_is_a_problem(tmp_path):
    """Every {Column} of the heading must be a column of the source table."""
    transpose = {"label": "Butir", "heading": "{Sesi} fase {Fase}"}
    result, out = _run(tmp_path, files={"tabel-4-7-keamanan.md": SAFETY}, tables=[_entry("safety_evidence", "tabel-4-7-keamanan.md", transpose=transpose)])
    assert result.problems == ["table safety_evidence: the transpose heading uses Fase, which tabel-4-7-keamanan.md does not have"]
    assert not (out / "safety_evidence.md").exists()


def test_a_manual_table_writes_nothing_and_is_listed_with_its_note(tmp_path):
    """Tabel 4.3 is merged by hand, so prep leaves it to the author."""
    note = "rows S4 onward are merged by hand into the table of draft D E-IV-27"
    result, out = _run(tmp_path, tables=[_entry("session_map", "tabel-peta-sesi.md", manual=note)])
    assert [(table.token, table.note) for table in result.manual] == [("session_map", note)]
    assert result.tables == [] and result.problems == [] and not (out / "session_map.md").exists()


def test_each_table_goes_portrait_or_landscape_by_its_width_at_the_ten_point_floor(tmp_path):
    """558 twips fits portrait (3000), 5026 fits only landscape (6000), and 7539 fits neither, which is a problem."""
    files = {
        "kecil.md": _table(["A", "B"], ["1", "2"]),
        "dua.md": _table(["A", "B"], [IDENTIFIER, IDENTIFIER]),
        "tiga.md": _table(["A", "B", "C"], [IDENTIFIER, IDENTIFIER, IDENTIFIER]),
    }
    tables = [_entry("small", "kecil.md"), _entry("wide", "dua.md"), _entry("too_wide", "tiga.md")]
    result, out = _run(tmp_path, files=files, tables=tables, text_width_twips={"portrait": 3000, "landscape": 6000})
    assert [(table.token, table.needed_twips, table.orientation) for table in result.tables] == [
        ("small", 558, PORTRAIT),
        ("wide", 5026, LANDSCAPE),
        ("too_wide", 7539, TOO_WIDE),
    ]
    assert result.problems == ["table too_wide (Tabel too_wide) needs 7539 twips (13.3 cm) at 10 pt, more than the landscape text width of 6000 twips; revise its layout"]
    assert (out / LANDSCAPE_CAPTIONS).read_text(encoding="utf-8") == "Tabel wide\n"
    assert result.landscape_captions == ["Tabel wide"]


def test_the_landscape_command_quotes_every_caption():
    """The printed command runs the landscape pass for exactly these captions."""
    command = landscape_command(["Tabel 4.5 Hasil Failover per Iterasi", "Tabel 4.8 Hasil Failback per Iterasi"])
    assert command == (
        "python3 -m redline landscape --docx IN --out OUT --report REPORT --author NAME "
        "--at 'Tabel 4.5 Hasil Failover per Iterasi' 'Tabel 4.8 Hasil Failback per Iterasi'"
    )


def test_figures_are_copied_by_path_and_by_the_file_a_value_names_and_a_missing_one_is_a_problem(tmp_path):
    """source is a path below the root; source_value names a value whose text is a file in the results folder."""
    figures = [
        {"token": "gambar-3-1-alur-penelitian", "source": "docs/figures/alur.png"},
        {"token": "gambar-4-3-failover-s4", "source_value": "S4.fig.file"},
        {"token": "gambar-4-2-grafana", "source": "docs/figures/tidak-ada.png"},
        {"token": "gambar-4-4-penanda-tangan-s4", "source_value": "S4.fig.signers"},
        {"token": "gambar-kosong", "source_value": "S4.fig.kosong"},
    ]
    harness = HARNESS | {"values": HARNESS["values"] | {"S4.fig.kosong": ""}}
    layout_path, root = _repository(tmp_path, {}, harness, {"figures": figures})
    (root / "docs" / "figures").mkdir()
    (root / "docs" / "figures" / "alur.png").write_bytes(png_bytes(4, 2))
    (root / "experiments" / "results" / "gambar-failover-S5-iterasi-1.png").write_bytes(png_bytes(6, 3))
    result = prepare(layout_path, root, tmp_path / "out")
    out = tmp_path / "out"
    assert (out / "gambar-3-1-alur-penelitian.png").read_bytes() == png_bytes(4, 2)
    assert (out / "gambar-4-3-failover-s4.png").read_bytes() == png_bytes(6, 3)
    assert [(figure.token, figure.source) for figure in result.figures] == [
        ("gambar-3-1-alur-penelitian", "docs/figures/alur.png"),
        ("gambar-4-3-failover-s4", "S4.fig.file = gambar-failover-S5-iterasi-1.png"),
    ]
    assert result.problems == [
        f"figure gambar-4-2-grafana: {root / 'docs/figures/tidak-ada.png'} does not exist",
        "figure gambar-4-4-penanda-tangan-s4: the value S4.fig.signers is not among the values",
        "figure gambar-kosong: the value S4.fig.kosong is empty",
    ]


def test_draft_tokens_without_a_value_are_reported_with_the_harness_reason_and_where_they_appear(tmp_path):
    """A token in an edit's Text and a token only in the notes sections both count; a token with a value does not."""
    draft = tmp_path / "D-bab4.md"
    draft.write_text(
        "# D. edit list\n\n"
        "### E-IV-01 (closes: T-1)\nOperation: replace\nAnchor: Teks lama.\n"
        "Text:\nRata-rata {{DATA:S4.n}} iterasi, ketersediaan {{DATA:soak.availability}} persen.\nRationale: r\n\n"
        "## Data tokens\n\n- `{{DATA:drill.recovery_s}}`: waktu pemulihan manual.\n- `{{DATA:S4.n}}`: jumlah iterasi.\n",
        encoding="utf-8",
    )
    result, _ = _run(tmp_path, drafts=(draft,))
    assert [(token.name, token.drafts, token.in_edit_text, token.reason) for token in result.missing_tokens] == [
        ("drill.recovery_s", ("D-bab4.md",), False, "no value in the harness output"),
        ("soak.availability", ("D-bab4.md",), True, "no session has soak.json"),
    ]
    assert result.drafts == ("D-bab4.md",)


def test_a_source_file_missing_from_the_results_is_a_problem_and_the_other_tables_are_still_written(tmp_path):
    """The scratch results hold S5 to S9 only, so tables such as the drill table have no source file yet."""
    tables = [_entry("drill", "tabel-drill.md"), _entry("failover_summary", "tabel-failover.md")]
    result, out = _run(tmp_path, files={"tabel-failover.md": FAILOVER}, tables=tables)
    assert result.problems == ["table drill: tabel-drill.md is not in the results folder"]
    assert [table.token for table in result.tables] == ["failover_summary"] and (out / "failover_summary.md").exists()


def test_an_out_folder_that_holds_files_is_refused_before_anything_is_written(tmp_path):
    """--out must not exist yet or be empty; an empty existing folder is fine."""
    layout_path, root = _repository(tmp_path, {}, HARNESS, {})
    used = tmp_path / "used"
    used.mkdir()
    (used / "lama.md").write_text("| A |\n|---|\n", encoding="utf-8")
    with pytest.raises(PrepError, match="is not empty"):
        prepare(layout_path, root, used)
    assert [path.name for path in used.iterdir()] == ["lama.md"]
    empty = tmp_path / "empty"
    empty.mkdir()
    assert prepare(layout_path, root, empty).problems == []


def test_a_missing_draft_stops_the_run_before_any_file_is_written(tmp_path):
    """Drafts are read before the first write, so a wrong --drafts path leaves --out untouched for the next run."""
    layout_path, root = _repository(tmp_path, {"tabel-failover.md": FAILOVER}, HARNESS, {"tables": [_entry("t", "tabel-failover.md")]})
    out = tmp_path / "out"
    with pytest.raises(OSError):
        prepare(layout_path, root, out, drafts=[tmp_path / "tidak-ada.md"])
    assert not out.exists()
