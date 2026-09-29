# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rendering of streamed results into HTML fragments.

Fragments are rendered with the templates of the *instance's own* theme, so a
streamed result is indistinguishable from a result of the first page.  Two
things differ from a normal page render:

* the fragment template is shipped by this plugin, hence no theme has to be
  modified -- the loader below serves it for every theme folder name,
* the fragment is rendered standalone, so the context provided by
  :py:obj:`searx.webapp.render` is used
  (:py:obj:`searx.webapp.render` is what the results page uses as well).
"""

from __future__ import annotations

import html
import logging
import pathlib
import typing as t

from jinja2 import BaseLoader, TemplateNotFound

log = logging.getLogger("searx.plugins.lazy_results")

PACKAGE_DIR = pathlib.Path(__file__).parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"

FRAGMENT_TEMPLATE = "lazy_results_fragment.html"

FIRST_INDEX_BASE = 100000
"""The DOM ids of the first page (``#result-media-<index>``) are continued here."""


class LazyResultsTemplateLoader(BaseLoader):
    """Serve the fragment template, whatever the theme is called."""

    def get_source(self, environment, template: str):  # pylint: disable=unused-argument
        _theme, sep, name = template.rpartition("/")
        if not sep or name != FRAGMENT_TEMPLATE:
            raise TemplateNotFound(template)
        path = TEMPLATES_DIR / FRAGMENT_TEMPLATE
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as exc:  # pragma: no cover - defensive
            raise TemplateNotFound(template) from exc
        return source, str(path), lambda: True

    def list_templates(self) -> list[str]:  # pragma: no cover - never used by SearXNG
        return [FRAGMENT_TEMPLATE]


def install(app: t.Any) -> None:
    """Make the fragment template available to *app*."""

    from jinja2 import ChoiceLoader

    app.jinja_loader = ChoiceLoader([LazyResultsTemplateLoader(), app.jinja_loader])


def prepare(results: list[t.Any], query: str) -> None:
    """Escape/highlight and group *results* exactly like the first page does.

    :py:obj:`searx.webapp.search` escapes title and content before they are
    marked as safe in the templates -- a hostile engine must not be able to
    inject markup through a streamed result either, so the very same
    transformation is applied here.
    """

    from searx.webapp import highlight_content

    previous_result = None
    current_template = None

    for result in results:
        if "content" in result and result["content"]:
            result["content"] = highlight_content(html.escape(result["content"][:1024]), query)
        if "title" in result and result["title"]:
            result["title"] = highlight_content(html.escape(result["title"] or ""), query)

        if current_template != result.template:
            result.open_group = True
            if previous_result is not None:
                previous_result.close_group = True
        current_template = result.template
        previous_result = result

    if previous_result is not None:
        previous_result.close_group = True


def only_template(results: list[t.Any]) -> bool:
    return len({result.template for result in results}) == 1


def render(results: list[t.Any], first_index: int = FIRST_INDEX_BASE) -> str:
    """Render *results* (already prepared, see :py:obj:`prepare`) to HTML."""

    from searx.webapp import render as webapp_render

    return webapp_render(
        FRAGMENT_TEMPLATE,
        lazy_results=results,
        lazy_first_index=first_index,
        lazy_only_template=only_template(results),
    )


__all__ = [
    "FRAGMENT_TEMPLATE",
    "FIRST_INDEX_BASE",
    "STATIC_DIR",
    "TEMPLATES_DIR",
    "install",
    "only_template",
    "prepare",
    "render",
]
