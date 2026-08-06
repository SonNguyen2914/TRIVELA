"""The warning about the engine signature must survive, and must not be
written where writing it would trip the thing it warns about.

TASK-20 asked for a header comment at the top of each of the four
signature modules. That order is self-defeating and these tests are why:
`engine_signature()` sha256s RAW BYTES, so adding the comment moves the
hash, fails both arms of the boot approval check, and darkens the plane —
the exact outcome the comment exists to prevent. Measured, not assumed:
prepending one comment line to `xg_model.py` moved the signature from
`b054833…` to `ff76bc8…`.

So the warning lives in the two places that can hold it:

  the guard's ASSERTION MESSAGE   read at the moment the edit is made
  a SIBLING MARKDOWN file         read by anyone browsing the directory

and these pin both against quiet deletion.
"""
import ast
import hashlib
import inspect
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MARKER = _ROOT / "src" / "models" / "ENGINE-SIGNATURE.md"

ENGINE_FILES = ("src/live/model_mls.py", "src/models/simulator.py",
                "src/models/xg_model.py", "src/models/features.py")


class TestTheClaimItselfIsTrue:
    """The warning asserts that a comment moves the hash. If that ever
    stopped being true the warning would be folklore, so check it."""

    def test_a_bare_comment_changes_a_signature_modules_digest(self):
        p = _ROOT / "src" / "models" / "xg_model.py"
        raw = p.read_bytes()
        before = hashlib.sha256(raw).hexdigest()
        after = hashlib.sha256(b"# a comment\n" + raw).hexdigest()
        assert before != after, (
            "the signature would no longer observe comments — the whole "
            "warning would need rewriting, not the test relaxing")

    def test_the_signature_hashes_bytes_not_parsed_code(self):
        """If it ever hashed a normalised AST instead, comments and
        docstrings would stop mattering and this rule would relax. That
        is a governance change for Son, not a refactor — but the test
        should notice it happened."""
        from src.live import model_mls
        src = inspect.getsource(model_mls.engine_signature)
        assert "fh.read()" in src and "sha256" in src


class TestTheWarningIsWhereItCanBeRead:
    def test_the_guards_message_explains_the_consequence(self):
        """A guard that only says 'this changed' teaches nothing to the
        person who changed it."""
        src = (_ROOT / "tests" / "test_team_style.py").read_text()
        body = src[src.index(
            "def test_engine_signature_modules_are_byte_identical_to_base"):]
        body = body[:body.index("\ndef ", 1)]
        for phrase in ("RAW BYTES", "DARK", "DO NOT PASTE",
                       "sibling module", "approval/activate"):
            assert phrase in body, f"the guard stopped saying {phrase!r}"

    def test_the_guard_names_which_module_moved(self):
        """Four hashes in a dict diff is a puzzle; a name is an answer."""
        src = (_ROOT / "tests" / "test_team_style.py").read_text()
        assert "ENGINE-SIGNATURE MODULE CHANGED" in src
        assert "changed = sorted(" in src

    def test_the_sibling_marker_exists_and_names_all_four(self):
        assert _MARKER.exists()
        text = _MARKER.read_text()
        for f in ENGINE_FILES:
            assert Path(f).name in text, f"{f} missing from the marker"

    def test_the_marker_says_why_it_is_not_inside_the_modules(self):
        """Without that sentence the file looks like an oversight and
        someone helpfully moves its contents into a header comment."""
        text = _MARKER.read_text()
        assert "would be the edit" in text


class TestItDidNotBreakTheRuleWhileDocumentingIt:
    def test_no_signature_module_was_touched_by_this_change(self):
        """The whole point. `tests/test_team_style.py` asserts the hashes
        independently; this states the intent in the file that would be
        edited if someone ever 'improved' the marker into a comment."""
        from tests.test_team_style import ENGINE_SOURCE_SHA256
        from src.live import model_mls
        assert model_mls.engine_signature()["source_sha256"] == \
            ENGINE_SOURCE_SHA256

    def test_the_marker_is_not_python_and_cannot_be_imported_by_them(self):
        assert _MARKER.suffix == ".md"

    def test_no_signature_module_gained_an_import_of_anything_new(self):
        """A marker that tempted someone into `from . import warning`
        would defeat itself. Parsed, not grepped — the modules' own
        docstrings may legitimately mention these names."""
        for rel in ENGINE_FILES:
            tree = ast.parse((_ROOT / rel).read_text())
            names = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names.update(a.name for a in node.names)
                elif isinstance(node, ast.ImportFrom):
                    names.add(node.module or "")
            assert not any("ENGINE" in n.upper() or "revision" in n
                           for n in names), (rel, names)


def test_agents_md_carries_the_rule_in_the_modelling_section():
    """§4 has it as a deployment consequence; §13 is where someone about
    to change a model actually looks."""
    text = (_ROOT / "AGENTS.md").read_text()
    section = text[text.index("## 13."):]
    assert "engine-signature modules" in section
    assert re.search(r"model_mls\.py", section)
