#!/usr/bin/env python
# Copyright (c) 2013-2014 Will Thames <will@thames.id.au>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
"""Command line implementation."""

import errno
import logging
import os
import pathlib
import subprocess
import sys
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, List, Set, Type, Union

from rich.markdown import Markdown
from rich.syntax import Syntax

from ansiblelint import cli, formatters
from ansiblelint.color import console, console_stderr
from ansiblelint.file_utils import cwd
from ansiblelint.generate_docs import rules_as_rich, rules_as_rst
from ansiblelint.rules import RulesCollection
from ansiblelint.runner import Runner
from ansiblelint.utils import get_playbooks_and_roles, get_rules_dirs

if TYPE_CHECKING:
    from argparse import Namespace

    from ansiblelint.errors import MatchError

_logger = logging.getLogger(__name__)

_rule_format_map = {
    'plain': str,
    'rich': rules_as_rich,
    'rst': rules_as_rst
}


def initialize_logger(level: int = 0) -> None:
    """Set up the global logging level based on the verbosity number."""
    VERBOSITY_MAP = {
        0: logging.NOTSET,
        1: logging.INFO,
        2: logging.DEBUG
    }

    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(levelname)-8s %(message)s')
    handler.setFormatter(formatter)
    logger = logging.getLogger(__package__)
    logger.addHandler(handler)
    # Unknown logging level is treated as DEBUG
    logging_level = VERBOSITY_MAP.get(level, logging.DEBUG)
    logger.setLevel(logging_level)
    # Use module-level _logger instance to validate it
    _logger.debug("Logging initialized to level %s", logging_level)


def choose_formatter_factory(
    options_list: "Namespace"
) -> Type[formatters.BaseFormatter]:
    """Select an output formatter based on the incoming command line arguments."""
    r: Type[formatters.BaseFormatter] = formatters.Formatter
    if options_list.quiet:
        r = formatters.QuietFormatter
    elif options_list.parseable:
        r = formatters.ParseableFormatter
    elif options_list.parseable_severity:
        r = formatters.ParseableSeverityFormatter
    return r


def report_outcome(matches: List["MatchError"], options) -> int:
    """Display information about how to skip found rules.

    Returns exit code, 2 if errors were found, 0 when only warnings were found.
    """
    failure = False
    msg = """\
You can skip specific rules or tags by adding them to your configuration file:
```yaml
# .ansible-lint
warn_list:  # or 'skip_list' to silence them completely
"""
    matches_unignored = [match for match in matches if not match.ignored]

    matched_rules = {match.rule.id: match.rule for match in matches_unignored}
    for id in sorted(matched_rules.keys()):
        if {id, *matched_rules[id].tags}.isdisjoint(options.warn_list):
            msg += f"  - '{id}'  # {matched_rules[id].shortdesc}\n"
            failure = True
    for match in matches:
        if "experimental" in match.rule.tags:
            msg += "  - experimental  # all rules tagged as experimental\n"
            break
    msg += "```"

    if matches and not options.quiet:
        console_stderr.print(Markdown(msg))

    if failure:
        return 2
    else:
        return 0


def main() -> int:
    """Linter CLI entry point."""
    cwd = pathlib.Path.cwd()

    options = cli.get_config(sys.argv[1:])

    initialize_logger(options.verbosity)
    _logger.debug("Options: %s", options)

    formatter_factory = choose_formatter_factory(options)
    formatter = formatter_factory(cwd, options.display_relative_path)

    rulesdirs = get_rules_dirs([str(rdir) for rdir in options.rulesdir],
                               options.use_default_rules)
    rules = RulesCollection(rulesdirs)

    if options.listrules:
        console.print(
            _rule_format_map[options.format](rules),
            highlight=False)
        return 0

    if options.listtags:
        console.print(
            Syntax(rules.listtags(), 'yaml')
            )
        return 0

    if isinstance(options.tags, str):
        options.tags = options.tags.split(',')

    skip = set()
    for s in options.skip_list:
        skip.update(str(s).split(','))
    options.skip_list = frozenset(skip)

    matches = _get_matches(rules, options)

    # Assure we do not print duplicates and the order is consistent
    matches = sorted(set(matches))

    mark_as_success = False
    if matches and options.progressive:
        _logger.info(
            "Matches found, running again on previous revision in order to detect regressions")
        with _previous_revision():
            old_matches = _get_matches(rules, options)
            # remove old matches from current list
            matches_delta = list(set(matches) - set(old_matches))
            if len(matches_delta) == 0:
                _logger.warning(
                    "Total violations not increased since previous "
                    "commit, will mark result as success. (%s -> %s)",
                    len(old_matches), len(matches_delta))
                mark_as_success = True

            ignored = 0
            for match in matches:
                # if match is not new, mark is as ignored
                if match not in matches_delta:
                    match.ignored = True
                    ignored += 1
            if ignored:
                _logger.warning(
                    "Marked %s previously known violation(s) as ignored due to"
                    " progressive mode.", ignored)

    _render_matches(matches, options, formatter, cwd)

    if matches and not mark_as_success:
        return report_outcome(matches, options=options)
    else:
        return 0


def _render_matches(
        matches: List,
        options: "Namespace",
        formatter: Any,
        cwd: Union[str, pathlib.Path]):

    ignored_matches = [match for match in matches if match.ignored]
    fatal_matches = [match for match in matches if not match.ignored]
    # Displayed ignored matches first
    if ignored_matches:
        _logger.warning(
            "Listing %s violation(s) marked as ignored, likely already known",
            len(ignored_matches))
        for match in ignored_matches:
            if match.ignored:
                print(formatter.format(match, options.colored))
    if fatal_matches:
        _logger.warning("Listing %s violation(s) that are fatal", len(fatal_matches))
        for match in fatal_matches:
            if not match.ignored:
                print(formatter.format(match, options.colored))

    # If run under GitHub Actions we also want to emit output recognized by it.
    if os.getenv('GITHUB_ACTIONS') == 'true' and os.getenv('GITHUB_WORKFLOW'):
        formatter = formatters.AnnotationsFormatter(cwd, True)
        for match in matches:
            print(formatter.format(match))


def _get_matches(rules: RulesCollection, options: "Namespace") -> list:

    if not options.playbook:
        # no args triggers auto-detection mode
        playbooks = get_playbooks_and_roles(options=options)
    else:
        playbooks = sorted(set(options.playbook))

    matches = list()
    checked_files: Set[str] = set()
    for playbook in playbooks:
        runner = Runner(rules, playbook, options.tags,
                        options.skip_list, options.exclude_paths,
                        options.verbosity, checked_files)
        matches.extend(runner.run())
    return matches


@contextmanager
def _previous_revision():
    """Create or update a temporary workdir containing the previous revision."""
    worktree_dir = ".cache/old-rev"
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD^1"],
        check=True,
        universal_newlines=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        ).stdout
    p = pathlib.Path(worktree_dir)
    p.mkdir(parents=True, exist_ok=True)
    os.system(f"git worktree add -f {worktree_dir} 2>/dev/null")
    with cwd(worktree_dir):
        os.system(f"git checkout {revision}")
        yield


if __name__ == "__main__":
    try:
        sys.exit(main())
    except IOError as exc:
        if exc.errno != errno.EPIPE:
            raise
    except RuntimeError as e:
        raise SystemExit(str(e))
