# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lazy (progressive) loading of search results for SearXNG.

SearXNG renders the result page only when every selected engine has answered --
or when the timeout of the slowest engine expired.  A single slow engine
therefore keeps the user in front of a blank page for several seconds.

This plugin splits that wait in two:

* the **first paint** renders the page as soon as the fast engines answered and
  lists the engines that are still busy,
* an ``EventSource`` then streams the results of the remaining engines into the
  already rendered page, engine by engine.

The plugin is self contained: it adds no dependency to SearXNG and degrades to a
no-op (``init()`` returns ``False``) when the internals it relies on are not
available.
"""

__version__ = "1.0.0"

__all__ = ["SXNGPlugin", "__version__"]


def __getattr__(name: str):
    """Import the plugin lazily.

    ``searxng_lazy_results.plugin`` is only imported when SearXNG's plugin
    storage asks for it, so importing (and unit testing) the other modules of
    this package does not require a SearXNG installation.
    """

    if name == "SXNGPlugin":
        from .plugin import SXNGPlugin  # pylint: disable=import-outside-toplevel

        return SXNGPlugin
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
