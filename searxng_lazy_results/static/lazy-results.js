// SPDX-License-Identifier: AGPL-3.0-or-later
/* Client of the searxng-lazy-results plugin.
 *
 * Listens on the `EventSource` of the plugin and appends the results of the
 * slow engines to the already rendered page.  Streamed results are rendered by
 * the server with the templates of the instance, this script only moves them
 * into the DOM -- and keeps the rest of the page honest: duplicated results are
 * dropped and the "Messages from the search engines" table is updated.
 */
(() => {
  "use strict";

  const configElement = document.getElementById("lazy-results-config");
  if (!configElement) {
    return;
  }

  let config;
  try {
    config = JSON.parse(configElement.textContent || "{}");
  } catch (error) {
    console.warn("lazy-results: unusable configuration", error);
    return;
  }

  const engines = Array.isArray(config.engines) ? config.engines.map(normalise).filter(Boolean) : [];
  if (!config.streamUrl || engines.length === 0 || typeof window.EventSource !== "function") {
    return;
  }

  const urlsElement = document.getElementById("urls");
  if (!urlsElement) {
    return;
  }

  const strings = config.strings || {};
  const withLabels = config.engineLabels !== false;
  const withDedupe = config.deduplicate !== false;
  const watchdogMs = 45000;

  const noResultsElement = urlsElement.querySelector(".dialog-error-block");
  const statsTable = document.getElementById("engines_msg-table");
  const knownUrls = new Set();
  const pending = new Set(engines.map((engine) => engine.name));
  const failed = new Map();

  let receivedEvents = 0;
  let addedResults = 0;
  let connectionErrors = 0;
  let finished = false;
  let watchdog = null;

  const panel = createPanel();
  insertBeforeResults(panel);
  collectKnownResults();
  collapseDuplicates();
  refreshPanel();
  armWatchdog();

  const source = new window.EventSource(config.streamUrl);
  source.addEventListener("engine", onEngine);
  source.addEventListener("done", onDone);
  source.addEventListener("error", onConnectionProblem);

  function normalise(entry) {
    if (Array.isArray(entry) && entry.length > 0) {
      return { name: String(entry[0]), category: String(entry[1] || "") };
    }
    if (typeof entry === "string") {
      return { name: entry, category: "" };
    }
    return null;
  }

  function text(value, fallback) {
    return typeof value === "string" && value !== "" ? value : fallback;
  }

  function createPanel() {
    const panelElement = document.createElement("div");
    panelElement.id = "lazy-results-panel";
    panelElement.className = "lazy-results-panel";
    panelElement.setAttribute("role", "status");
    panelElement.setAttribute("aria-live", "polite");

    const spinner = document.createElement("span");
    spinner.className = "lazy-results-spinner";
    spinner.setAttribute("aria-hidden", "true");

    const label = document.createElement("span");
    label.className = "lazy-results-panel-text";

    panelElement.append(spinner, label);
    return panelElement;
  }

  function insertBeforeResults(node) {
    const anchor = noResultsElement && noResultsElement.parentNode === urlsElement ? noResultsElement : panel;
    if (anchor && anchor.parentNode === urlsElement) {
      urlsElement.insertBefore(node, anchor);
      return;
    }
    urlsElement.appendChild(node);
  }

  function refreshPanel() {
    const label = panel.querySelector(".lazy-results-panel-text");
    if (!label || pending.size === 0) {
      return;
    }
    label.textContent = `${text(strings.loading, "Still loading results from slow engines")}: ${Array.from(pending).join(", ")}`;
  }

  function armWatchdog() {
    if (watchdog !== null) {
      window.clearTimeout(watchdog);
    }
    watchdog = window.setTimeout(() => {
      if (!finished) {
        close("timeout");
      }
    }, watchdogMs);
  }

  function collectKnownResults() {
    urlsElement.querySelectorAll("article.result").forEach((article) => {
      const key = resultKey(article);
      if (key) {
        knownUrls.add(key);
      }
    });
  }

  function resultKey(article) {
    const anchor = article.querySelector("a.url_header") || article.querySelector("h3 a[href]");
    const href = anchor && anchor.getAttribute("href");
    return href || null;
  }

  function onEngine(event) {
    let data;
    try {
      data = JSON.parse(event.data);
    } catch (error) {
      console.warn("lazy-results: unusable engine payload", error);
      return;
    }
    if (!data || !data.engine) {
      return;
    }

    receivedEvents += 1;
    armWatchdog();
    pending.delete(data.engine);

    if (data.status === "ok" && typeof data.html === "string" && data.html.trim() !== "") {
      const added = appendBatch(data);
      if (added > 0) {
        addedResults += added;
        hideNoResults();
      }
      markEngineCell(data, "resolved");
    } else if (data.status !== "empty") {
      const message = text(data.error, text(strings.failed, "failed"));
      failed.set(data.engine, message);
      markEngineCell(data, "failed");
    }

    refreshPanel();
  }

  function appendBatch(data) {
    const template = document.createElement("template");
    template.innerHTML = data.html.trim();

    const batch = document.createElement("section");
    batch.className = "lazy-results-batch";
    batch.dataset.engine = data.engine;
    batch.append(...Array.from(template.content.childNodes));

    let added = 0;
    batch.querySelectorAll("article.result").forEach((article) => {
      const key = resultKey(article);
      if (withDedupe && key && knownUrls.has(key)) {
        article.remove();
        return;
      }
      if (key) {
        knownUrls.add(key);
      }
      added += 1;
    });

    if (added === 0) {
      return 0;
    }

    batch.querySelectorAll("div[class^='template_group_']").forEach((group) => {
      if (!group.querySelector("article.result")) {
        group.remove();
      }
    });

    if (withLabels) {
      batch.prepend(createLabel(data, added));
    }
    insertBeforeResults(batch);
    return added;
  }

  function createLabel(data, added) {
    const label = document.createElement("p");
    label.className = "lazy-results-batch-label";

    const engine = document.createElement("strong");
    engine.textContent = data.engine;

    const meta = document.createElement("span");
    const parts = [];
    if (typeof data.elapsed === "number") {
      parts.push(`${data.elapsed} ${text(strings.seconds, "s")}`);
    }
    parts.push(added === 1 ? text(strings.resultOne, "1 result") : text(strings.resultsMany, "{count} results").replace("{count}", String(added)));
    meta.textContent = ` \u00b7 ${parts.join(" \u00b7 ")}`;

    label.append(document.createTextNode(`${text(strings.resultsFrom, "Results from")} `), engine, meta);
    return label;
  }

  function hideNoResults() {
    if (noResultsElement) {
      noResultsElement.classList.add("lazy-results-hidden");
    }
  }

  function markEngineCell(data, kind) {
    const rows = engineRows(data.engine);
    if (rows.length === 0) {
      return;
    }

    // SearXNG records the timeout of the first paint and the timeout of the
    // engine's own request as two separate messages -- collapse them into one.
    const [first, ...duplicates] = rows;
    duplicates.forEach((row) => row.remove());

    const cell = first.querySelector("td.response-error");
    if (!cell) {
      return;
    }
    cell.classList.add(`lazy-results-${kind}`);
    if (kind === "resolved") {
      cell.textContent = `${text(strings.loaded, "loaded")} \u00b7 ${data.elapsed} ${text(strings.seconds, "s")}`;
      return;
    }
    if (data.error) {
      cell.textContent = data.error;
    }
  }

  function engineRows(engine) {
    if (!statsTable) {
      return [];
    }
    return Array.from(statsTable.querySelectorAll("tr")).filter((row) => {
      const cell = row.querySelector("td.engine-name");
      return cell && cell.textContent.trim() === engine;
    });
  }

  function collapseDuplicates() {
    engines.forEach((engine) => {
      engineRows(engine.name)
        .slice(1)
        .forEach((row) => row.remove());
    });
  }

  function onDone() {
    finished = true;
    try {
      source.close();
    } catch (error) {
      /* ignore */
    }
    showOutcome();
  }

  function onConnectionProblem() {
    if (finished) {
      return;
    }
    connectionErrors += 1;

    if (source.readyState === window.EventSource.CONNECTING && receivedEvents === 0 && connectionErrors < 2) {
      // The browser retries on its own, give it exactly one chance: a second
      // attempt replays the engines of the first attempt on the server.
      refreshPanel();
      return;
    }
    close("connection");
  }

  function close(reason) {
    finished = true;
    try {
      source.close();
    } catch (error) {
      /* ignore */
    }
    for (const engine of engines) {
      if (pending.has(engine.name)) {
        failed.set(engine.name, reason === "timeout" ? text(strings.timedOut, "timeout") : text(strings.failed, "failed"));
      }
    }
    pending.clear();
    showOutcome();
  }

  function showOutcome() {
    if (watchdog !== null) {
      window.clearTimeout(watchdog);
      watchdog = null;
    }
    panel.remove();

    if (addedResults === 0 && noResultsElement) {
      noResultsElement.classList.remove("lazy-results-hidden");
    }
    if (failed.size === 0 || statsTable) {
      // The engines that failed are already listed in the sidebar table.
      return;
    }

    const note = document.createElement("p");
    note.className = "lazy-results-note";
    note.textContent = `${text(strings.failed, "failed")}: ${Array.from(failed.keys()).join(", ")}`;
    insertBeforeResults(note);
  }
})();
