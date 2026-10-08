"""Compile every template so a syntax error cannot reach production.

Django parses a Jinja2 template lazily, the first time it is rendered, and
``manage.py check`` does not render anything. A template with invalid syntax
therefore passes the system check, passes lint, passes the unit tests, and then
returns a 500 to a visitor. The reports page shipped that way for one commit
because ``{{ value|truncatechars:120 }}`` is Django filter syntax and the rest of
the project is Jinja2, where the colon is a parse error.

This test walks both template directories and compiles each file. It is the
cheapest possible guard against the whole class of defect and costs well under a
second.
"""

import pathlib

import pytest
from django.template.loader import get_template

JINJA_ROOT = pathlib.Path(__file__).resolve().parents[2] / "templates_jinja2"
DJANGO_ROOT = pathlib.Path(__file__).resolve().parents[2] / "apps" / "common" / "templates"


def _jinja_templates():
    return sorted(
        path.relative_to(JINJA_ROOT).as_posix() for path in JINJA_ROOT.rglob("*.html")
    )


def _django_templates():
    return sorted(
        path.relative_to(DJANGO_ROOT).as_posix() for path in DJANGO_ROOT.rglob("*.html")
    )


@pytest.mark.parametrize("template_name", _jinja_templates())
def test_jinja2_template_compiles(template_name):
    assert get_template(template_name) is not None


@pytest.mark.parametrize("template_name", _django_templates())
def test_django_template_compiles(template_name):
    assert get_template(template_name) is not None


def test_template_directories_are_not_empty():
    # Guards against the parametrize lists silently becoming empty, which would
    # turn this into a test that always passes while checking nothing.
    assert len(_jinja_templates()) > 20
    # The login page is the only template still on the Django template engine —
    # it is rendered by django.contrib.auth, not by a project view.
    assert _django_templates() == ["registration/login.html"]
