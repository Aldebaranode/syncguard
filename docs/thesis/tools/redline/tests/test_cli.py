"""Tests for the command-line layer: argument wiring, report output and exit codes."""

from pathlib import Path

from redline import cli
from redline.commands import CommandError, CommandResult


def test_apply_passes_every_edits_file_and_writes_the_report(tmp_path, monkeypatch, capsys):
    """--edits takes several paths and may repeat; the report goes to --report."""
    calls = []

    def fake_apply(docx, edit_paths, author, out):
        """Record the call and return a passing result."""
        calls.append((docx, edit_paths, author, out))
        return CommandResult("# report\n", True)

    monkeypatch.setattr(cli, "run_apply", fake_apply)
    report = tmp_path / "r.md"
    argv = ["apply", "--docx", "in.docx", "--edits", "a.md", "b.md", "--edits", "c.md",
            "--author", "Syahid Nurrohim", "--out", "out.docx", "--report", str(report)]
    assert cli.main(argv) == 0
    assert calls == [(Path("in.docx"), [Path("a.md"), Path("b.md"), Path("c.md")], "Syahid Nurrohim", Path("out.docx"))]
    assert report.read_text(encoding="utf-8") == "# report\n"
    assert capsys.readouterr().out == f"redline: wrote out.docx; report {report}\n"


def test_check_prints_the_report_and_fails_when_an_edit_fails(tmp_path, monkeypatch, capsys):
    """check prints the report; a failing edit gives exit code 1."""
    monkeypatch.setattr(cli, "run_check", lambda docx, edit_paths: CommandResult("# check\n", False))
    assert cli.main(["check", "--edits", "a.md", "--docx", "in.docx"]) == 1
    assert capsys.readouterr().out == "# check\n"


def test_fill_delegates_and_writes_the_report(tmp_path, monkeypatch):
    """fill passes docx, values and out through and writes the report."""
    calls = []

    def fake_fill(docx, values, out, author):
        """Record the call and return a passing result."""
        calls.append((docx, values, out, author))
        return CommandResult("# fill\n", True)

    monkeypatch.setattr(cli, "run_fill", fake_fill)
    report = tmp_path / "f.md"
    argv = ["fill", "--docx", "a.docx", "--values", "v", "--out", "b.docx", "--report", str(report), "--author", "S N"]
    assert cli.main(argv) == 0
    assert calls == [(Path("a.docx"), Path("v"), Path("b.docx"), "S N")]
    assert report.read_text(encoding="utf-8") == "# fill\n"


def test_command_errors_map_to_exit_code_two(monkeypatch, capsys):
    """Expected failures print one line on stderr and exit 2."""

    def refuse(docx, edit_paths):
        """Raise the error a real flow would raise."""
        raise CommandError("in.docx is not a Word document")

    monkeypatch.setattr(cli, "run_check", refuse)
    assert cli.main(["check", "--edits", "a.md", "--docx", "in.docx"]) == 2
    assert capsys.readouterr().err == "redline: in.docx is not a Word document\n"


def test_registry_delegates_and_writes_the_report(tmp_path, monkeypatch):
    """registry passes the draft paths and output through and writes the report."""
    calls = []

    def fake_registry(drafts, out):
        """Record the call and return a passing result."""
        calls.append((drafts, out))
        return CommandResult("# registry\n", True)

    monkeypatch.setattr(cli, "run_registry", fake_registry)
    report = tmp_path / "r.md"
    argv = ["registry", "--drafts", "A.md", "F.md", "--out", "data/references.json", "--report", str(report)]
    assert cli.main(argv) == 0
    assert calls == [([Path("A.md"), Path("F.md")], Path("data/references.json"))]
    assert report.read_text(encoding="utf-8") == "# registry\n"


def test_cite_delegates_with_author(tmp_path, monkeypatch):
    """cite passes docx, registry, out and author through and writes the report."""
    calls = []

    def fake_cite(docx, registry, out, author):
        """Record the call and return a failing result."""
        calls.append((docx, registry, out, author))
        return CommandResult("# cite\n", False)

    monkeypatch.setattr(cli, "run_cite", fake_cite)
    report = tmp_path / "c.md"
    argv = ["cite", "--docx", "in.docx", "--registry", "r.json", "--out", "o.docx", "--report", str(report), "--author", "S N"]
    assert cli.main(argv) == 1
    assert calls == [(Path("in.docx"), Path("r.json"), Path("o.docx"), "S N")]
    assert report.read_text(encoding="utf-8") == "# cite\n"


def test_italics_headers_and_restart_lists_delegate_with_their_options(tmp_path, monkeypatch):
    """Each new subcommand passes its arguments to its run function and writes the report."""
    calls = []
    monkeypatch.setattr(cli, "run_italics", lambda docx, terms, out, author: calls.append(("italics", docx, terms, out, author)) or CommandResult("# i\n", True))
    monkeypatch.setattr(cli, "run_headers", lambda docx, out, author, skip: calls.append(("headers", docx, out, author, skip)) or CommandResult("# h\n", True))
    monkeypatch.setattr(cli, "run_restart_lists", lambda docx, out, author, prefixes: calls.append(("lists", docx, out, author, prefixes)) or CommandResult("# l\n", True))
    report = tmp_path / "r.md"
    common = ["--docx", "a.docx", "--out", "b.docx", "--report", str(report), "--author", "S N"]
    assert cli.main(["italics", "--terms", "t.txt"] + common) == 0
    assert cli.main(["headers", "--skip-first-cell", "Kolom", "--skip-first-cell", "Ok-Chol"] + common) == 0
    assert cli.main(["restart-lists", "--at", "Node aktif", "Inspeksi berkas", "--at", "Pemeriksaan internal"] + common) == 0
    assert calls == [
        ("italics", Path("a.docx"), Path("t.txt"), Path("b.docx"), "S N"),
        ("headers", Path("a.docx"), Path("b.docx"), "S N", ("Kolom", "Ok-Chol")),
        ("lists", Path("a.docx"), Path("b.docx"), "S N", ["Node aktif", "Inspeksi berkas", "Pemeriksaan internal"]),
    ]


def test_terms_delegates_and_uses_the_shipped_exclusions_by_default(tmp_path, monkeypatch):
    """terms passes docx, drafts and out; --exclude defaults to data/term_exclusions.txt."""
    calls = []
    monkeypatch.setattr(cli, "run_terms", lambda docx, drafts, out, exclusions, additions: calls.append((docx, drafts, out, exclusions)) or CommandResult("# t\n", True))
    report = tmp_path / "r.md"
    argv = ["terms", "--docx", "a.docx", "--drafts", "A.md", "B.md", "--out", "terms.txt", "--report", str(report)]
    assert cli.main(argv) == 0
    [(docx, drafts, out, exclusions)] = calls
    assert (docx, drafts, out) == (Path("a.docx"), [Path("A.md"), Path("B.md")], Path("terms.txt"))
    assert exclusions.name == "term_exclusions.txt" and exclusions.parent.name == "data"


def test_breaks_delegates(tmp_path, monkeypatch):
    """breaks passes docx, out and author to run_breaks."""
    calls = []
    monkeypatch.setattr(cli, "run_breaks", lambda docx, out, author: calls.append((docx, out, author)) or CommandResult("# b\n", True))
    report = tmp_path / "r.md"
    assert cli.main(["breaks", "--docx", "a.docx", "--out", "b.docx", "--report", str(report), "--author", "S N"]) == 0
    assert calls == [(Path("a.docx"), Path("b.docx"), "S N")]


def test_landscape_delegates_with_every_prefix(tmp_path, monkeypatch):
    """landscape passes docx, out, author and every --at prefix to run_landscape; a failing run exits 1."""
    calls = []
    monkeypatch.setattr(cli, "run_landscape", lambda docx, out, author, prefixes: calls.append((docx, out, author, prefixes)) or CommandResult("# l\n", False))
    report = tmp_path / "r.md"
    common = ["--docx", "a.docx", "--out", "b.docx", "--report", str(report), "--author", "S N"]
    assert cli.main(["landscape", "--at", "Tabel 4.5 Hasil", "Tabel 4.8 Hasil", "--at", "Tabel 4.13 Bukti"] + common) == 1
    assert calls == [(Path("a.docx"), Path("b.docx"), "S N", ["Tabel 4.5 Hasil", "Tabel 4.8 Hasil", "Tabel 4.13 Bukti"])]
    assert report.read_text(encoding="utf-8") == "# l\n"


def test_prep_delegates_with_its_options(tmp_path, monkeypatch):
    """prep passes layout, root, out, the optional results folder and the drafts to run_prep."""
    calls = []
    monkeypatch.setattr(cli, "run_prep", lambda layout, root, out, results, drafts: calls.append((layout, root, out, results, drafts)) or CommandResult("# p\n", True))
    report = tmp_path / "r.md"
    common = ["--layout", "fill-layout.json", "--root", "/repo", "--out", "values", "--report", str(report)]
    assert cli.main(["prep"] + common + ["--results", "scratch/out", "--drafts", "A.md", "D.md"]) == 0
    assert cli.main(["prep"] + common) == 0
    assert calls == [
        (Path("fill-layout.json"), Path("/repo"), Path("values"), Path("scratch/out"), [Path("A.md"), Path("D.md")]),
        (Path("fill-layout.json"), Path("/repo"), Path("values"), None, []),
    ]
    assert report.read_text(encoding="utf-8") == "# p\n"
