"""Tests related to skip tag id."""
from ansiblelint.rules import RulesCollection
from ansiblelint.rules.yaml_rule import YamllintRule
from ansiblelint.runner import Runner
from ansiblelint.testing import run_ansible_lint

FILE = "examples/playbooks/with-skip-tag-id.yml"
collection = RulesCollection()
collection.register(YamllintRule())


def test_negative_no_param() -> None:
    """Negative test no param."""
    bad_runner = Runner(FILE, rules=collection)
    errs = bad_runner.run()
    assert len(errs) > 0


def test_negative_with_id() -> None:
    """Negative test with_id."""
    with_id = "yaml"
    bad_runner = Runner(FILE, rules=collection, tags=frozenset([with_id]))
    errs = bad_runner.run()
    assert len(errs) == 1


def test_negative_with_tag() -> None:
    """Negative test with_tag."""
    with_tag = "trailing-spaces"
    bad_runner = Runner(FILE, rules=collection, tags=frozenset([with_tag]))
    errs = bad_runner.run()
    assert len(errs) == 1


def test_positive_skip_id() -> None:
    """Positive test skip_id."""
    skip_id = "yaml"
    good_runner = Runner(FILE, rules=collection, skip_list=[skip_id])
    assert [] == good_runner.run()


def test_positive_skip_tag() -> None:
    """Positive test skip_tag."""
    skip_tag = "yaml[trailing-spaces]"
    good_runner = Runner(FILE, rules=collection, skip_list=[skip_tag])
    assert [] == good_runner.run()


def test_run_skip_warning_rule() -> None:
    """Test that we can skip warning[empty-playbook]."""
    result = run_ansible_lint(
        "-x",
        "warning[empty-playbook]",
        "examples/playbooks/empty_playbook.yml",
        executable="ansible-lint",
    )
    assert result.returncode == 0
    assert result.stdout == ""
