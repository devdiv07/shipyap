"""v0.1.4 release identity: version, the `shipyap` command with `pros` as its alias, and page titles.

Offline: reads pyproject.toml and renders pages in memory; nothing is installed or served.
"""

import tomllib
from pathlib import Path

import pytest

from shipyap.automation import cli
from shipyap.review.pages import layout, message_page, queue_page

ROOT = Path(__file__).resolve().parents[1]
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def test_the_release_version_is_0_1_4():
    assert PROJECT["version"] == "0.1.4"


def test_readme_has_project_badges_and_keeps_the_screenshot_placeholder():
    readme = (ROOT / "README.md").read_text("utf-8")
    badges = readme.split("**You ship.")[0]
    clone = next(line for line in readme.splitlines() if line.startswith("git clone https://"))
    repo = clone.removeprefix("git clone https://github.com/").removesuffix(".git")
    assert badges.startswith("# Shipyap\n\n")
    assert f"{repo}/actions/workflows/ci.yml/badge.svg" in badges
    assert f"github/v/release/{repo}" in badges
    assert "[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)" in badges
    assert "<!-- Screenshot placeholder:" in readme


def test_post_text_uses_proportional_font_while_source_pre_keeps_monospace():
    html = layout("Review", "")
    assert ".post pre { font:17px/1.65 system-ui, sans-serif; max-width:65ch" in html
    assert "pre { white-space: pre-wrap;" in html
    assert "font: 14px/1.5 ui-monospace,\n  monospace;" in html


def test_shipyap_and_the_pros_alias_run_the_same_code():
    scripts = PROJECT["scripts"]
    assert scripts["shipyap"] == "shipyap.automation.cli:main"
    assert scripts["pros"] == scripts["shipyap"]
    assert set(scripts) == {"shipyap", "pros"}


def test_the_distribution_and_package_are_named_shipyap():
    import shipyap

    assert PROJECT["name"] == "shipyap"
    assert Path(shipyap.__file__).resolve().parent == ROOT / "src" / "shipyap"
    assert not (ROOT / "src" / "public_research_os").exists()


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
