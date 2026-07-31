"""Every league model must accept the call `model_eval` actually makes.

La Liga's `ensure_model_version()` took no argument while its three
siblings took `approved_for_shadow`. `model_eval` calls all of them with
the keyword, so the La Liga branch raised TypeError — but only in
production, because locally the replay ladder refuses with "dormant"
before it ever reaches that line. A signature check costs nothing and
catches the whole class.
"""
import inspect

import pytest

MODULES = ["model_mls", "model_epl", "model_ligamx", "model_laliga"]


@pytest.mark.parametrize("name", MODULES)
def test_accepts_the_keyword_model_eval_passes(name):
    mod = __import__(f"src.live.{name}", fromlist=[name])
    fn = getattr(mod, "ensure_model_version")
    sig = inspect.signature(fn)
    assert "approved_for_shadow" in sig.parameters, (
        f"{name}.ensure_model_version cannot accept the call model_eval "
        f"makes: {sig}")


@pytest.mark.parametrize("name", MODULES)
def test_defaults_to_dark_where_a_default_exists(name):
    """Fail-closed: an unqualified call must never approve."""
    mod = __import__(f"src.live.{name}", fromlist=[name])
    p = inspect.signature(getattr(mod, "ensure_model_version")).parameters
    d = p["approved_for_shadow"].default
    assert d in (inspect.Parameter.empty, False), (
        f"{name} defaults to {d!r} — an unqualified call would approve it")


def test_the_flag_is_assigned_outside_the_row_creation_branch():
    """The flag must be settable on an EXISTING row, not only a new one.

    La Liga assigned it inside `if row is None`, so a revocation would
    have reported success and left the model approved.

    Checked on the AST, not with a regex over the source — the first
    attempt at this test used one, and it swallowed the rest of the
    function body, reporting all four modules as broken including the
    three that were correct.
    """
    import ast
    for name in MODULES:
        mod = __import__(f"src.live.{name}", fromlist=[name])
        tree = ast.parse(inspect.getsource(
            getattr(mod, "ensure_model_version")))
        fn = tree.body[0]

        def assigns(nodes):
            return any(
                isinstance(n, ast.Attribute)
                and n.attr == "approved_for_shadow"
                and isinstance(n.ctx, ast.Store)
                for node in nodes for n in ast.walk(node))

        creation = [n for n in ast.walk(fn) if isinstance(n, ast.If)
                    and "row is None" in ast.unparse(n.test)]
        assert creation, f"{name}: no `row is None` branch found"
        inside = assigns(creation[0].body)
        # the statements that are NOT inside that branch
        inner = {id(x) for n in creation for x in ast.walk(n)}
        outside = assigns([n for n in fn.body if id(n) not in inner])
        assert outside, (
            f"{name} never assigns approved_for_shadow outside the "
            f"row-creation branch, so an existing row can never be updated")
        assert not inside, (
            f"{name} assigns approved_for_shadow inside `if row is None`")
