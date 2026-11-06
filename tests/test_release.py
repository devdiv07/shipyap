"""v0.1.0 release identity: version, the `shipyap` command with `pros` as its alias, and page titles.

Offline: reads pyproject.toml and renders pages in memory; nothing is installed or served.
"""

import tomllib
from pathlib import Path

import pytest

from public_research_os.automation import cli
from public_research_os.review.pages import layout, message_page, queue_page

ROOT = Path(__file__).resolve().parents[1]
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def test_the_release_version_is_0_1_0():
    assert PROJECT["version"] == "0.1.0"


def test_shipyap_and_the_pros_alias_run_the_same_code():
    scripts = PROJECT["scripts"]
    assert scripts["shipyap"] == "public_research_os.automation.cli:main"
    assert scripts["pros"] == scripts["shipyap"]
    assert set(scripts) == {"shipyap", "pros"}


def test_help_names_the_shipyap_command(capsys):
    with pytest.raises(SystemExit) as raised:
        cli.main(["--help"])
    assert raised.value.code == 0
    assert capsys.readouterr().out.startswith("usage: shipyap ")


def test_an_unknown_command_is_refused_under_the_shipyap_name(capsys):
    with pytest.raises(SystemExit) as raised:
        cli.main(["publish-everything"])
    assert raised.value.code == 2
    err = capsys.readouterr().err
    assert "shipyap: error:" in err and "publish-everything" not in err


@pytest.mark.parametrize(
    ("demo", "suffix"),
    [(False, " · Shipyap</title>"), (True, " · Shipyap demo</title>")],
    ids=["review", "demo"],
)
def test_review_and_demo_page_titles_say_shipyap(demo, suffix):
    assert f"<title>Drafts{suffix}" in queue_page([], [], demo=demo)
    assert f"<title>Not found{suffix}" in message_page(404, "Not found", "x", demo=demo).html


def test_a_page_title_stays_escaped_with_the_brand():
    assert "<title>&lt;b&gt; · Shipyap</title>" in layout("<b>", "")
