"""Invocation guards in run.py.

These are argparse-level refusals, checked BEFORE logging is set up and long
before anything is fetched or sent, so they run without touching the network,
the filesystem or the API. run.py is otherwise untested glue on purpose (the
transaction rules live in deliver.py where they can be tested properly) --
but the mutual-exclusion rules are safety properties, not glue.
"""
from __future__ import annotations

import pytest

from pipeline.run import main


def _run(monkeypatch, *argv) -> int:
    monkeypatch.setattr("sys.argv", ["pipeline.run", *argv])
    with pytest.raises(SystemExit) as e:
        main()
    return e.value.code


def test_apply_refuses_a_truncated_source_list(monkeypatch, capsys):
    assert _run(monkeypatch, "--apply", "--limit-sources", "3") == 2
    assert "partial digest" in capsys.readouterr().err


# --- --render-only replays PAST judgments, from a cache with no age bound ---

@pytest.mark.parametrize("flag", ["--apply", "--commit-seen", "--sync"])
def test_render_only_refuses_flags_that_act_on_the_world(monkeypatch, capsys, flag):
    """--apply would mail out a fortnight-old digest; --commit-seen would
    permanently bury items on the strength of a replay. --sync is merely
    meaningless, but accepting-and-ignoring reads as if it did something."""
    assert _run(monkeypatch, "--render-only", flag) == 2
    err = capsys.readouterr().err
    assert flag in err
    assert "sends nothing and commits nothing" in err


def test_render_only_refuses_limit_sources(monkeypatch, capsys):
    assert _run(monkeypatch, "--render-only", "--limit-sources", "3") == 2
    assert "--limit-sources" in capsys.readouterr().err


def test_the_refusal_names_every_conflicting_flag_at_once(monkeypatch, capsys):
    """Not just the first one found -- fixing them one error at a time is a
    worse experience than being told all three."""
    code = _run(monkeypatch, "--render-only", "--apply", "--commit-seen", "--sync")
    assert code == 2
    err = capsys.readouterr().err
    assert "--apply" in err and "--commit-seen" in err and "--sync" in err
