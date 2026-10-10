"""End-to-end: apply draft A to a copy of the real manuscript and check the result.

The manuscript is copied into a temporary directory first; the original file is only read.
validate.py needs defusedxml; when the validator's interpreter cannot import it (set
REDLINE_VALIDATE_PYTHONPATH to a directory that holds it), the validation test is skipped.
"""

import hashlib
import shutil
import zipfile
from pathlib import Path

import pytest
from lxml import etree

from redline.apply import run_edits
from redline.edits import load_edit_files
from redline.matcher import index_paragraphs
from redline.package import WordPackage
from redline.validation import run_validate
from redline.wordml import qn

THESIS = Path(__file__).resolve().parents[3]
MANUSCRIPT = THESIS / "manuscript" / "BAB1-5-REVISI-TELAAH-drive-original.docx"
DRAFT_A = THESIS / "drafts" / "A-front-bab1.md"
DRAFTS_ABC = [DRAFT_A, THESIS / "drafts" / "B-bab2.md", THESIS / "drafts" / "C-bab3.md"]
AUTHOR = "Syahid Nurrohim"
DATE = "2026-10-05T08:00:00Z"


def _edit_count(paths: list[Path]) -> int:
    """Number of well-formed edit blocks in the drafts as they are now (the drafts keep changing)."""
    return len(load_edit_files(paths).edits)


pytestmark = pytest.mark.skipif(not (MANUSCRIPT.is_file() and DRAFT_A.is_file()), reason="manuscript or draft A not present")


@pytest.fixture(scope="module")
def applied(tmp_path_factory):
    """Copy the manuscript, apply draft A, save the result; return paths and outcomes."""
    manuscript_hash = hashlib.sha256(MANUSCRIPT.read_bytes()).hexdigest()
    work = tmp_path_factory.mktemp("e2e")
    original = work / "original.docx"
    shutil.copyfile(MANUSCRIPT, original)
    out = work / "revised.docx"
    package = WordPackage.load(original)
    outcomes = run_edits(package, [DRAFT_A], AUTHOR, DATE)
    package.save(out)
    return {"original": original, "out": out, "outcomes": outcomes, "manuscript_hash": manuscript_hash}


def _document(path: Path) -> etree._Element:
    """Parse word/document.xml of a package."""
    with zipfile.ZipFile(path) as archive:
        return etree.fromstring(archive.read("word/document.xml"))


def _has_revision(paragraph: etree._Element) -> bool:
    """True when the paragraph holds any w:ins or w:del (its runs or its mark)."""
    return next(paragraph.iter(qn("ins"), qn("del")), None) is not None


def test_every_edit_of_draft_a_applies(applied):
    """Every edit of draft A applies; E-1-27's 'List' resolves to Normal through the alias map."""
    outcomes = applied["outcomes"]
    failures = [(outcome.edit_id, [problem.message for problem in outcome.problems]) for outcome in outcomes if outcome.problems]
    assert failures == []
    assert len(outcomes) == _edit_count([DRAFT_A])
    assert len({outcome.edit_id for outcome in outcomes}) == len(outcomes)


def test_validate_reports_no_untracked_change(applied):
    """validate.py with --original and --author passes on the revised copy."""
    result = run_validate(applied["out"], applied["original"], AUTHOR)
    if "No module named 'defusedxml'" in result.output:
        pytest.skip("validate.py needs defusedxml; set REDLINE_VALIDATE_PYTHONPATH")
    assert result.passed, result.output
    assert "All validations PASSED!" in result.output


def test_untouched_paragraphs_and_their_hyperlinks_are_byte_identical(applied):
    """Paragraphs no edit replaced or deleted keep their exact XML, Zotero hyperlinks included."""
    touched = {outcome.matched.number for outcome in applied["outcomes"] if outcome.operation in ("replace", "delete")}
    before = [item.element for item in index_paragraphs(_document(applied["original"]).find(qn("body"))) if item.number not in touched]
    after = [item.element for item in index_paragraphs(_document(applied["out"]).find(qn("body"))) if not _has_revision(item.element)]
    assert [etree.tostring(p) for p in after] == [etree.tostring(p) for p in before]
    links_before = [etree.tostring(link) for p in before for link in p.iter(qn("hyperlink"))]
    links_after = [etree.tostring(link) for p in after for link in p.iter(qn("hyperlink"))]
    assert links_after == links_before
    assert len(links_before) > 0


def test_revision_counts_match_the_outcomes(applied):
    """The number of w:ins and w:del elements in the output equals the reported counts."""
    document = _document(applied["out"])
    assert len(list(document.iter(qn("ins")))) == sum(outcome.inserted for outcome in applied["outcomes"])
    assert len(list(document.iter(qn("del")))) == sum(outcome.deleted for outcome in applied["outcomes"])


def test_other_parts_are_unchanged_and_the_manuscript_is_untouched(applied):
    """Only word/document.xml differs (and word/numbering.xml when an edit starts a new list); the source keeps its hash."""
    starts_a_list = any(edit.numbering is not None and edit.numbering.mode == "new" for edit in load_edit_files([DRAFT_A]).edits)
    expected = {"word/document.xml", "word/numbering.xml"} if starts_a_list else {"word/document.xml"}
    with zipfile.ZipFile(applied["original"]) as before, zipfile.ZipFile(applied["out"]) as after:
        assert before.namelist() == after.namelist()
        changed = {name for name in before.namelist() if before.read(name) != after.read(name)}
    assert changed == expected
    assert hashlib.sha256(MANUSCRIPT.read_bytes()).hexdigest() == applied["manuscript_hash"]


@pytest.fixture(scope="module")
def combined(tmp_path_factory):
    """Apply drafts A, B and C together to one copy of the manuscript."""
    work = tmp_path_factory.mktemp("e2e_abc")
    original = work / "original.docx"
    shutil.copyfile(MANUSCRIPT, original)
    out = work / "revised-abc.docx"
    package = WordPackage.load(original)
    outcomes = run_edits(package, DRAFTS_ABC, AUTHOR, DATE)
    package.save(out)
    return {"original": original, "out": out, "outcomes": outcomes}


def test_drafts_a_b_and_c_apply_together_without_errors(combined):
    """Every edit of A, B and C applies to one copy; warnings are allowed, errors are not."""
    outcomes = combined["outcomes"]
    failures = [(outcome.edit_id, [problem.message for problem in outcome.problems]) for outcome in outcomes if outcome.problems]
    assert failures == []
    assert len(outcomes) == _edit_count(DRAFTS_ABC)
    assert len({outcome.edit_id for outcome in outcomes}) == len(outcomes)


def test_combined_result_passes_validate_and_counts_match(combined):
    """validate.py passes on the A+B+C result and the revision counts equal the outcomes."""
    document = _document(combined["out"])
    assert len(list(document.iter(qn("ins")))) == sum(outcome.inserted for outcome in combined["outcomes"])
    assert len(list(document.iter(qn("del")))) == sum(outcome.deleted for outcome in combined["outcomes"])
    result = run_validate(combined["out"], combined["original"], AUTHOR)
    if "No module named 'defusedxml'" in result.output:
        pytest.skip("validate.py needs defusedxml; set REDLINE_VALIDATE_PYTHONPATH")
    assert result.passed, result.output
