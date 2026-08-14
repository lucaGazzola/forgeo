/* Central dashboard script: renders the home instance list and the
   per-instance page (kanban backlog + logs/history/blocker/config tabs).
   Plain JS, no frameworks, refreshes every 30 seconds. */

(function () {
  "use strict";

  var REFRESH_MS = 30000;
  var TIMEOUT_MS = 5000;
  var STATUS_ORDER = ["OPEN", "BLOCKED", "COMPLETED", "FAILED"];
  var TABS = ["backlog", "create", "logs", "history", "blocker", "config"];

  /* Optional bearer auth: the token lives in localStorage under TOKEN_KEY.
     When the dashboard requires one (any /api/* call answers 401), the user
     is sent to the token prompt (LOGIN_PATH) unless a token is already
     stored — a `?token=...` URL also signs in automatically. */
  var TOKEN_KEY = "forgeo.web.token";
  var LOGIN_PATH = "/central/login.html";

  /* Board compaction: non-OPEN columns collapse behind a count + expand
     toggle once they exceed COLLAPSE_MIN_TASKS, and every column renders at
     most MAX_VISIBLE_PER_COLUMN cards (the most recent) until "show more" is
     clicked. Expanded state survives the 30s auto-refresh. */
  var COLLAPSE_MIN_TASKS = 4;
  var MAX_VISIBLE_PER_COLUMN = 20;
  var COLLAPSED_BY_DEFAULT = { BLOCKED: true, COMPLETED: true, FAILED: true };
  var expandedColumns = {};
  var showAllColumns = {};

  /* Backlog search + status filter: the search box matches id/title/
     description substrings and the status select narrows which columns are
     shown. Both live in the URL as ?q=... and ?status=... so a view survives
     reloads and can be shared. While a filter is active every match is shown
     (no column compaction), so searching a large backlog never hides results
     behind "show more". */
  var filterQuery = "";
  var filterStatus = "all";

  /* Run history pagination: one page of recent runs at a time, newest first.
     runsPage is the current page index (0 = the newest page) and survives the
     30s auto-refresh, like the board's expanded-column state. */
  var RUNS_PAGE_SIZE = 25;
  var runsPage = 0;

  var page = document.body.dataset.page || "home";
  var match = page === "instance" ? location.pathname.match(/^\/instances\/([^/]+)\/?/) : null;
  var instanceName = match ? decodeURIComponent(match[1]) : null;
  var API = instanceName ? "/api/instances/" + encodeURIComponent(instanceName) + "/" : null;
  var currentTab = "backlog";
  var instanceStatus = null;
  var configDirty = false;
  var configFormBuilt = false;

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) {
      node.textContent = String(text);
    }
    return node;
  }

  function setText(id, text) {
    var node = document.getElementById(id);
    if (node) node.textContent = text;
  }

  function formatTime(iso) {
    if (!iso) return "—";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function formatInterval(minutes) {
    if (minutes === null || minutes === undefined) return "—";
    if (minutes === 1) return "1 min";
    return minutes + " mins";
  }

  function timeEl(label, iso) {
    var span = el("span", null, label + " ");
    var time = el("time", null, formatTime(iso));
    time.dateTime = iso || "";
    span.appendChild(time);
    return span;
  }

  function getStoredToken() {
    try {
      return localStorage.getItem(TOKEN_KEY) || "";
    } catch (e) {
      return "";
    }
  }

  function storeToken(token) {
    try {
      localStorage.setItem(TOKEN_KEY, token);
    } catch (e) {}
  }

  function clearStoredToken() {
    try {
      localStorage.removeItem(TOKEN_KEY);
    } catch (e) {}
  }

  function authHeaders(extra) {
    var headers = extra || {};
    var token = getStoredToken();
    if (token) headers.Authorization = "Bearer " + token;
    return headers;
  }

  function goToLogin(returnUrl) {
    if (location.pathname.indexOf(LOGIN_PATH) === 0) return;
    var next = returnUrl ? encodeURIComponent(returnUrl) : "";
    window.location.href = LOGIN_PATH + "?next=" + next;
  }

  /* fetch() that always attaches the stored bearer token (when present) and
     sends 401 responses to the token prompt — the login flow is the only
     static page that must work without a token. */
  function apiFetch(url, options) {
    options = options || {};
    options.headers = authHeaders(options.headers || {});
    return fetch(url, options).then(function (resp) {
      if (resp.status === 401) {
        clearStoredToken();
        goToLogin(url);
        throw new Error("unauthorized");
      }
      return resp;
    });
  }

  function fetchJSON(url) {
    var controller = typeof AbortController === "function" ? new AbortController() : null;
    var timer = controller ? setTimeout(function () { controller.abort(); }, TIMEOUT_MS) : null;
    var opts = controller ? { signal: controller.signal } : {};
    return apiFetch(url, opts)
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .finally(function () {
        if (timer) clearTimeout(timer);
      });
  }

  function setDown(down) {
    var notice = document.getElementById("daemon-notice");
    if (notice) notice.hidden = !down;
    var ft = document.getElementById("fetch-time");
    if (ft && ft.parentElement) {
      ft.parentElement.dataset.stale = down ? "true" : "false";
    }
  }

  function stampFetchTime() {
    setText("fetch-time", new Date().toLocaleTimeString());
  }

  /* ------------------------------------------------------------------ */
  /* Home page                                                           */
  /* ------------------------------------------------------------------ */

  function renderHome(instances) {
    var list = document.getElementById("instance-list");
    var empty = document.getElementById("empty-state");
    if (!list) return;
    list.textContent = "";
    setText("meta-count", String(instances.length));
    if (instances.length === 0) {
      if (empty) empty.hidden = false;
      return;
    }
    if (empty) empty.hidden = true;

    instances.forEach(function (inst) {
      var card = el("a", "instance-card", null);
      card.href = "instances/" + encodeURIComponent(inst.name) + "/";

      var head = el("div", "instance-card__head");
      head.appendChild(el("span", "instance-card__name", inst.name));
      head.appendChild(
        el(
          "span",
          "badge badge--" + (inst.daemon_running ? "COMPLETED" : "FAILED"),
          inst.daemon_running ? "running" : "stopped"
        )
      );
      card.appendChild(head);

      var grid = el("div", "instance-card__grid");

      var info = el("div", "instance-card__info");
      info.appendChild(el("span", "instance-card__label", "repo"));
      info.appendChild(el("span", "instance-card__value", inst.repo || "(unavailable)"));
      grid.appendChild(info);

      info = el("div", "instance-card__info");
      info.appendChild(el("span", "instance-card__label", "last outcome"));
      info.appendChild(el("span", "instance-card__value", inst.last_outcome || "—"));
      grid.appendChild(info);

      info = el("div", "instance-card__info");
      info.appendChild(el("span", "instance-card__label", "next run"));
      info.appendChild(el("span", "instance-card__value", formatTime(inst.next_run_at)));
      grid.appendChild(info);

      var counts = el("div", "instance-card__counts");
      STATUS_ORDER.forEach(function (status) {
        counts.appendChild(
          el(
            "span",
            "count-chip count-chip--" + status,
            status + " " + (inst.backlog_counts[status] || 0)
          )
        );
      });
      grid.appendChild(counts);

      card.appendChild(grid);
      list.appendChild(card);
    });
  }

  function refreshHome() {
    fetchJSON("/api/instances")
      .then(function (data) {
        renderHome(data);
        setDown(false);
        stampFetchTime();
      })
      .catch(function () {
        setDown(true);
      });
  }

  /* ------------------------------------------------------------------ */
  /* Instance page: kanban backlog                                       */
  /* ------------------------------------------------------------------ */

  function buildColumns() {
    var board = document.getElementById("backlog-board");
    if (!board) return;
    STATUS_ORDER.forEach(function (status) {
      var col = document.createElement("section");
      col.className = "status-col";
      col.dataset.status = status;

      var head = el("div", "status-col__head");
      var label = el("div", "status-col__label");
      label.appendChild(el("span", "status-col__dot"));
      label.appendChild(el("span", "status-col__name", status));
      head.appendChild(label);
      head.appendChild(el("span", "status-col__count", "0"));

      var list = el("div", "status-col__list");
      col.appendChild(head);
      col.appendChild(list);
      board.appendChild(col);
    });
  }

  function createTaskCard(task, status) {
    var card = el("article", "task");
    card.setAttribute("tabindex", "0");
    card.setAttribute("role", "button");
    card.addEventListener("click", function () {
      openModal(task, card);
    });
    card.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openModal(task, card);
      }
    });
    var top = el("div", "task__top");
    top.appendChild(el("span", "task__id", task.id));
    top.appendChild(el("span", "badge badge--" + status, status));
    if (status === "FAILED" && task.retry_count) {
      top.appendChild(el("span", "badge badge--retry", "retried " + task.retry_count + "x"));
    }
    card.appendChild(top);
    card.appendChild(el("h3", "task__title", task.title));
    if (task.description) {
      card.appendChild(el("p", "task__desc", task.description));
    }
    var times = el("div", "task__times");
    times.appendChild(timeEl("created", task.created_at));
    times.appendChild(timeEl("updated", task.updated_at));
    card.appendChild(times);
    return card;
  }

  function isColumnCollapsed(status, count) {
    return (
      COLLAPSED_BY_DEFAULT[status] &&
      count > COLLAPSE_MIN_TASKS &&
      !expandedColumns[status]
    );
  }

  function expandColumnButton(status, count, list, group) {
    var btn = el("button", "status-col__expand", null);
    btn.setAttribute("type", "button");
    btn.setAttribute("aria-expanded", "false");
    btn.appendChild(el("span", "status-col__expand-label", "show " + status.toLowerCase()));
    btn.appendChild(el("span", "status-col__expand-count", String(count)));
    btn.addEventListener("click", function () {
      expandedColumns[status] = true;
      renderColumn(list, status, group);
    });
    return btn;
  }

  function collapseColumnButton(status, list, group) {
    var btn = el("button", "status-col__collapse", null);
    btn.setAttribute("type", "button");
    btn.setAttribute("aria-expanded", "true");
    btn.appendChild(el("span", "status-col__collapse-label", "hide " + status.toLowerCase()));
    btn.addEventListener("click", function () {
      expandedColumns[status] = false;
      showAllColumns[status] = false;
      renderColumn(list, status, group);
    });
    return btn;
  }

  function showMoreButton(status, older, list, group) {
    var btn = el("button", "status-col__more", null);
    btn.setAttribute("type", "button");
    btn.setAttribute("aria-expanded", "false");
    btn.textContent = "show " + older.length + " more";
    btn.addEventListener("click", function () {
      showAllColumns[status] = true;
      older.forEach(function (task) {
        list.insertBefore(createTaskCard(task, status), btn.nextSibling);
      });
      if (COLLAPSED_BY_DEFAULT[status] && group.length > COLLAPSE_MIN_TASKS) {
        list.insertBefore(collapseColumnButton(status, list, group), btn);
      }
      btn.parentNode.removeChild(btn);
    });
    return btn;
  }

  function renderColumn(list, status, group) {
    list.textContent = "";
    if (group.length === 0) {
      list.appendChild(el("p", "status-col__empty", "nothing here"));
      return;
    }

    var filterActive = isFilterActive();

    if (!filterActive && isColumnCollapsed(status, group.length)) {
      list.appendChild(expandColumnButton(status, group.length, list, group));
      return;
    }

    var visible = group;
    var older = [];
    if (!filterActive && group.length > MAX_VISIBLE_PER_COLUMN && !showAllColumns[status]) {
      older = group.slice(0, group.length - MAX_VISIBLE_PER_COLUMN);
      visible = group.slice(older.length);
    }

    if (older.length > 0) {
      list.appendChild(showMoreButton(status, older, list, group));
    } else if (!filterActive && COLLAPSED_BY_DEFAULT[status] && group.length > COLLAPSE_MIN_TASKS) {
      list.appendChild(collapseColumnButton(status, list, group));
    }

    visible.forEach(function (task) {
      list.appendChild(createTaskCard(task, status));
    });
  }

  function isFilterActive() {
    return filterQuery.trim() !== "" || filterStatus !== "all";
  }

  function filterTasks(tasks) {
    var q = filterQuery.trim().toLowerCase();
    return tasks.filter(function (task) {
      var status = (task.status || "OPEN").toUpperCase();
      if (filterStatus !== "all" && status !== filterStatus) return false;
      if (!q) return true;
      var haystack = (
        (task.id || "") + " " +
        (task.title || "") + " " +
        (task.description || "")
      ).toLowerCase();
      return haystack.indexOf(q) !== -1;
    });
  }

  function renderTasks(tasks) {
    var board = document.getElementById("backlog-board");
    var empty = document.getElementById("empty-state");
    var noMatches = document.getElementById("no-matches-state");
    if (!board) return;
    allTasks = tasks || [];
    var filtered = filterTasks(allTasks);
    var filterActive = isFilterActive();

    if (empty) empty.hidden = allTasks.length > 0;
    if (noMatches) {
      noMatches.hidden = !(allTasks.length > 0 && filterActive && filtered.length === 0);
    }
    var countNode = document.getElementById("backlog-count");
    if (countNode) {
      var showing = filterActive && allTasks.length > 0;
      countNode.hidden = !showing;
      if (showing) {
        countNode.textContent = filtered.length + " of " + allTasks.length + " tasks";
      }
    }

    STATUS_ORDER.forEach(function (status) {
      var col = board.querySelector('.status-col[data-status="' + status + '"]');
      if (!col) return;
      var list = col.querySelector(".status-col__list");
      var count = col.querySelector(".status-col__count");
      var group = filtered.filter(function (t) {
        return (t.status || "OPEN").toUpperCase() === status;
      });
      col.hidden = filterStatus !== "all" && status !== filterStatus;
      count.textContent = String(group.length);
      renderColumn(list, status, group);
    });

    syncModal(allTasks);
  }

  /* Backlog filter wiring: read ?q= and ?status= from the URL on load and
     push changes back into it so filters survive reloads and are shareable. */
  function readFilters() {
    var params = new URLSearchParams(location.search);
    filterQuery = params.get("q") || "";
    var status = params.get("status") || "all";
    filterStatus = STATUS_ORDER.indexOf(status) !== -1 ? status : "all";
    var search = document.getElementById("backlog-search");
    if (search) search.value = filterQuery;
    var statusSelect = document.getElementById("backlog-status");
    if (statusSelect) statusSelect.value = filterStatus;
  }

  function syncFilterUrl() {
    var params = new URLSearchParams(location.search);
    if (filterQuery) params.set("q", filterQuery);
    else params.delete("q");
    if (filterStatus !== "all") params.set("status", filterStatus);
    else params.delete("status");
    var search = params.toString();
    var url = location.pathname + (search ? "?" + search : "") + location.hash;
    history.replaceState(null, "", url);
  }

  function wireBacklogFilters() {
    var search = document.getElementById("backlog-search");
    if (search) {
      search.addEventListener("input", function () {
        filterQuery = search.value;
        syncFilterUrl();
        renderTasks(allTasks);
      });
    }
    var statusSelect = document.getElementById("backlog-status");
    if (statusSelect) {
      statusSelect.addEventListener("change", function () {
        filterStatus = statusSelect.value;
        syncFilterUrl();
        renderTasks(allTasks);
      });
    }
  }

  /* ------------------------------------------------------------------ */
  /* Task detail modal                                                   */
  /* ------------------------------------------------------------------ */

  var modalTaskId = null;
  var modalTask = null;
  var modalLastFocus = null;
  var allTasks = [];

  function listItems(values) {
    var ul = el("ul", "modal__list");
    if (!values || values.length === 0) {
      ul.appendChild(el("li", "modal__empty", "—"));
    } else {
      values.forEach(function (value) {
        ul.appendChild(el("li", null, value));
      });
    }
    return ul;
  }

  function taskById(id) {
    for (var i = 0; i < allTasks.length; i++) {
      if (allTasks[i].id === id) return allTasks[i];
    }
    return null;
  }

  function computeUnsatisfied(task) {
    /* Fallback for task objects that carry no server-computed
       unsatisfied_dependencies (e.g. a PATCH/POST response): resolve each
       dependency against the full backlog we already have. */
    var unmet = [];
    (task.dependencies || []).forEach(function (depId) {
      var dep = taskById(depId);
      var status = dep ? (dep.status || "OPEN").toUpperCase() : "missing";
      if (status !== "COMPLETED") {
        unmet.push({ id: depId, status: status });
      }
    });
    return unmet;
  }

  function showModalSection(id, show) {
    var node = document.getElementById(id);
    if (node) node.hidden = !show;
  }

  function isOpenOrBlocked(status) {
    return status === "OPEN" || status === "BLOCKED";
  }

  function updateModalActions() {
    if (!modalTask) return;
    var status = (modalTask.status || "OPEN").toUpperCase();
    showModalSection("task-modal-edit", isOpenOrBlocked(status));
    var deleteBtn = document.getElementById("task-modal-delete");
    if (deleteBtn) deleteBtn.hidden = !isOpenOrBlocked(status);
    var reopenBtn = document.getElementById("task-modal-reopen");
    if (reopenBtn) reopenBtn.hidden = status !== "BLOCKED";
  }

  function renderModal(task) {
    if (!task) return;
    modalTask = task;
    setText("task-modal-id", task.id);
    setText("task-modal-title", task.title || "");
    var badge = document.getElementById("task-modal-status");
    if (badge) {
      var status = (task.status || "OPEN").toUpperCase();
      badge.textContent = status;
      badge.className = "badge badge--" + status;
    }

    var status = (task.status || "OPEN").toUpperCase();
    updateModalActions();

    var blocked = status === "BLOCKED";
    var failed = status === "FAILED";
    showModalSection("task-modal-blocker-section", blocked);
    showModalSection("task-modal-failure-section", failed);
    if (blocked) {
      var count = document.getElementById("task-modal-blocked-count");
      if (count) count.textContent = "blocked " + (task.blocked_count || 0) + "x";
      var reason = document.getElementById("task-modal-blocker-reason");
      if (reason) {
        var lines = task.blocker_reason || [];
        reason.textContent = lines.length
          ? lines.join("\n")
          : "The agent did not explain what it needs.";
      }
    }
    if (failed) {
      var failReason = document.getElementById("task-modal-failure-reason");
      if (failReason) {
        var failLines = task.failure_reason || [];
        failReason.textContent = failLines.length
          ? failLines.join("\n")
          : "The agent errored; no reason was recorded.";
      }
      var failCount = document.getElementById("task-modal-failed-count");
      if (failCount) {
        var bits = [];
        if (task.retry_count) bits.push("retried " + task.retry_count + "x");
        if (typeof task.retry_budget === "number" && task.retry_budget > 0) {
          if (task.retries_remaining > 0) {
            bits.push(
              task.retries_remaining + " retr" + (task.retries_remaining === 1 ? "y" : "ies") + " left"
            );
          } else {
            bits.push("retries exhausted — reopen manually to retry");
          }
        } else if (task.retries_left === 0) {
          bits.push("retries disabled for this task");
        }
        failCount.textContent = bits.join(" · ");
        failCount.hidden = bits.length === 0;
      }
    }

    setText("task-modal-description", task.description || "");
    var acceptance = document.getElementById("task-modal-acceptance");
    if (acceptance) {
      acceptance.textContent = "";
      acceptance.appendChild(listItems(task.acceptance_criteria));
    }
    var dependencies = document.getElementById("task-modal-dependencies");
    if (dependencies) {
      dependencies.textContent = "";
      dependencies.appendChild(listItems(task.dependencies));
    }
    var unmet = Array.isArray(task.unsatisfied_dependencies)
      ? task.unsatisfied_dependencies
      : computeUnsatisfied(task);
    var unmetList = document.getElementById("task-modal-unmet-dependencies");
    if (unmetList) {
      unmetList.textContent = "";
      unmetList.appendChild(
        listItems(
          unmet.map(function (dep) {
            return dep.id + " — " + dep.status;
          })
        )
      );
    }
    showModalSection("task-modal-unmet-dependencies-section", unmet.length > 0);
    var files = document.getElementById("task-modal-files");
    if (files) {
      files.textContent = "";
      files.appendChild(listItems(task.files_to_modify));
    }
    var command = document.getElementById("task-modal-command");
    if (command) {
      command.textContent = Array.isArray(task.agent_command)
        ? task.agent_command.join(" ")
        : task.agent_command || "";
    }
    var retries = document.getElementById("task-modal-retries");
    if (retries) {
      var retryText = null;
      if (task.retries_left !== null && task.retries_left !== undefined) {
        var overrideBudget = typeof task.retry_budget === "number" ? task.retry_budget : task.retries_left;
        retryText = "retries left: " + task.retries_left + " of " + overrideBudget + " (per-task override)";
      }
      retries.textContent = retryText || "";
    }
    var created = document.getElementById("task-modal-created");
    if (created) created.textContent = formatTime(task.created_at);
    var updated = document.getElementById("task-modal-updated");
    if (updated) updated.textContent = formatTime(task.updated_at);

    showModalSection("task-modal-description-section", Boolean(task.description));
    showModalSection(
      "task-modal-acceptance-section",
      task.acceptance_criteria && task.acceptance_criteria.length > 0
    );
    showModalSection(
      "task-modal-dependencies-section",
      task.dependencies && task.dependencies.length > 0
    );
    showModalSection(
      "task-modal-files-section",
      task.files_to_modify && task.files_to_modify.length > 0
    );
    showModalSection("task-modal-command-section", Boolean(task.agent_command));
    showModalSection(
      "task-modal-retries-section",
      Boolean(retries && retries.textContent)
    );
  }

  function splitLines(value) {
    return String(value)
      .split(/\r?\n/)
      .map(function (line) {
        return line.trim();
      })
      .filter(Boolean);
  }

  function enterEditMode() {
    if (!modalTask) return;
    var setValue = function (id, text) {
      var node = document.getElementById(id);
      if (node) node.value = text;
    };
    setValue("task-edit-title", modalTask.title || "");
    setValue("task-edit-description", modalTask.description || "");
    setValue("task-edit-acceptance", (modalTask.acceptance_criteria || []).join("\n"));
    setValue("task-edit-dependencies", (modalTask.dependencies || []).join("\n"));
    setValue("task-edit-files", (modalTask.files_to_modify || []).join("\n"));
    setValue(
      "task-edit-command",
      Array.isArray(modalTask.agent_command)
        ? modalTask.agent_command.join(" ")
        : modalTask.agent_command || ""
    );
    setValue(
      "task-edit-timeout",
      modalTask.agent_timeout_seconds === null || modalTask.agent_timeout_seconds === undefined
        ? ""
        : String(modalTask.agent_timeout_seconds)
    );
    setValue(
      "task-edit-retries",
      modalTask.retries_left === null || modalTask.retries_left === undefined
        ? ""
        : String(modalTask.retries_left)
    );

    showModalSection("task-modal-view", false);
    showModalSection("task-modal-edit-form", true);
    showModalSection("task-modal-edit", false);
    var deleteBtn = document.getElementById("task-modal-delete");
    if (deleteBtn) deleteBtn.hidden = true;
    var reopenBtn = document.getElementById("task-modal-reopen");
    if (reopenBtn) reopenBtn.hidden = true;
    var error = document.getElementById("task-modal-error");
    if (error) error.hidden = true;
  }

  function exitEditMode() {
    showModalSection("task-modal-view", true);
    showModalSection("task-modal-edit-form", false);
    updateModalActions();
    var error = document.getElementById("task-modal-error");
    if (error) error.hidden = true;
  }

  function collectEditForm() {
    var value = function (id) {
      var node = document.getElementById(id);
      return node ? node.value : "";
    };
    var command = value("task-edit-command").trim();
    var timeout = value("task-edit-timeout").trim();
    var retries = value("task-edit-retries").trim();
    var updates = {
      title: value("task-edit-title").trim(),
      description: value("task-edit-description").trim(),
      acceptance_criteria: splitLines(value("task-edit-acceptance")),
      dependencies: splitLines(value("task-edit-dependencies")),
      files_to_modify: splitLines(value("task-edit-files")),
      agent_command: command ? command : null,
      agent_timeout_seconds: timeout === "" ? null : Number(timeout),
      retries_left: retries === "" ? null : parseInt(retries, 10),
    };
    return updates;
  }

  function saveTask() {
    if (!API || !modalTaskId) return;
    var error = document.getElementById("task-modal-error");
    if (error) error.hidden = true;

    var title = document.getElementById("task-edit-title");
    if (!title || !title.value.trim()) {
      if (error) {
        error.textContent = "title is required";
        error.hidden = false;
      }
      return;
    }
    var timeout = document.getElementById("task-edit-timeout");
    if (timeout && timeout.value.trim() !== "" && isNaN(Number(timeout.value))) {
      if (error) {
        error.textContent = "agent timeout must be a number";
        error.hidden = false;
      }
      return;
    }
    var retries = document.getElementById("task-edit-retries");
    if (retries && retries.value.trim() !== "") {
      var retriesNum = parseInt(retries.value.trim(), 10);
      if (isNaN(retriesNum) || retriesNum < 0 || String(retriesNum) !== retries.value.trim()) {
        if (error) {
          error.textContent = "retries left must be a non-negative integer";
          error.hidden = false;
        }
        return;
      }
    }

    apiFetch(API + "tasks/" + encodeURIComponent(modalTaskId), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(collectEditForm()),
    })
      .then(function (resp) {
        if (!resp.ok) {
          return resp.json().then(function (data) {
            throw new Error((data && data.error) || "HTTP " + resp.status);
          });
        }
        return resp.json();
      })
      .then(function (task) {
        renderModal(task);
        exitEditMode();
        return fetchJSON(API + "tasks");
      })
      .then(function (tasks) {
        renderTasks(tasks || []);
      })
      .catch(function (err) {
        if (error) {
          error.textContent = err.message || "failed to save task";
          error.hidden = false;
        }
      });
  }

  function deleteTask() {
    if (!API || !modalTaskId) return;
    if (!window.confirm("Delete this task? This cannot be undone.")) return;
    var error = document.getElementById("task-modal-delete-error");
    if (error) error.hidden = true;

    apiFetch(API + "tasks/" + encodeURIComponent(modalTaskId), {
      method: "DELETE",
    })
      .then(function (resp) {
        if (!resp.ok) {
          return resp.json().then(function (data) {
            throw new Error((data && data.error) || "HTTP " + resp.status);
          });
        }
        return resp.json();
      })
      .then(function () {
        closeModal();
        return fetchJSON(API + "tasks");
      })
      .then(function (tasks) {
        renderTasks(tasks || []);
      })
      .catch(function (err) {
        if (error) {
          error.textContent = err.message || "failed to delete task";
          error.hidden = false;
        }
      });
  }

  function reopenTask() {
    if (!API || !modalTaskId) return;
    var error = document.getElementById("task-modal-error");
    if (error) error.hidden = true;
    var reopenBtn = document.getElementById("task-modal-reopen");
    if (reopenBtn) reopenBtn.disabled = true;

    apiFetch(API + "tasks/" + encodeURIComponent(modalTaskId) + "/reopen", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    })
      .then(function (resp) {
        if (!resp.ok) {
          return resp.json().then(function (data) {
            throw new Error((data && data.error) || "HTTP " + resp.status);
          });
        }
        return resp.json();
      })
      .then(function (task) {
        renderModal(task);
        return fetchJSON(API + "tasks");
      })
      .then(function (tasks) {
        renderTasks(tasks || []);
      })
      .catch(function (err) {
        if (error) {
          error.textContent = err.message || "failed to reopen task";
          error.hidden = false;
        }
      })
      .finally(function () {
        if (reopenBtn) reopenBtn.disabled = false;
      });
  }

  function openModal(task, opener) {
    if (!task) return;
    modalTaskId = task.id;
    modalLastFocus = opener || document.activeElement;
    var deleteError = document.getElementById("task-modal-delete-error");
    if (deleteError) deleteError.hidden = true;
    exitEditMode();
    renderModal(task);
    var modal = document.getElementById("task-modal");
    if (modal) {
      modal.hidden = false;
      var dialog = modal.querySelector(".modal");
      if (dialog) dialog.focus();
    }
  }

  function closeModal() {
    var modal = document.getElementById("task-modal");
    if (!modal || modal.hidden) return;
    modal.hidden = true;
    modalTaskId = null;
    if (modalLastFocus && modalLastFocus.focus) modalLastFocus.focus();
    modalLastFocus = null;
  }

  function syncModal(tasks) {
    if (!modalTaskId) return;
    var found = tasks.filter(function (t) {
      return t.id === modalTaskId;
    });
    if (found.length === 0) {
      closeModal();
    } else {
      renderModal(found[0]);
    }
  }

  function renderStatus(status) {
    instanceStatus = status || {};
    setText("forgeo-name", status.name || instanceName);
    var daemon = Boolean(status.daemon_running);
    var badge = document.getElementById("meta-daemon");
    if (badge) {
      badge.textContent = daemon ? "running" : "stopped";
      badge.className = "daemon-badge daemon-badge--" + (daemon ? "running" : "stopped");
    }
    setText("meta-repo", status.repo || "—");
    setText("meta-interval", formatInterval(status.interval_minutes));
    setText("meta-next", formatTime(status.next_run_at));
    setText("meta-outcome", status.last_outcome || "—");
    updateDaemonButtons();
  }

  /* ------------------------------------------------------------------ */
  /* Instance page: daemon lifecycle actions (start / stop / restart)    */
  /* ------------------------------------------------------------------ */

  function updateDaemonButtons() {
    var running = Boolean(instanceStatus && instanceStatus.daemon_running);
    var start = document.getElementById("daemon-start");
    var stop = document.getElementById("daemon-stop");
    var restart = document.getElementById("daemon-restart");
    if (start) start.disabled = running;
    if (stop) stop.disabled = !running;
    if (restart) restart.disabled = false;
  }

  function setDaemonBusy(busy) {
    ["daemon-start", "daemon-stop", "daemon-restart"].forEach(function (id) {
      var btn = document.getElementById(id);
      if (btn) btn.disabled = busy;
    });
  }

  function showDaemonFeedback(message, isError) {
    var node = document.getElementById("daemon-feedback");
    if (!node) return;
    node.textContent = message || "";
    node.classList.toggle("daemon-feedback--error", Boolean(isError));
    node.hidden = !message;
  }

  function daemonAction(action) {
    if (!API) return;
    setDaemonBusy(true);
    showDaemonFeedback(action + "…", false);
    apiFetch(API + action, { method: "POST" })
      .then(function (resp) {
        return resp.json().then(function (data) {
          if (!resp.ok) {
            var err = new Error((data && (data.error || data.message)) || "HTTP " + resp.status);
            throw err;
          }
          return data;
        });
      })
      .then(function (data) {
        showDaemonFeedback(data.message || data.status || action + " succeeded", false);
      })
      .catch(function (err) {
        showDaemonFeedback(err.message || action + " failed", true);
      })
      .finally(function () {
        setDaemonBusy(false);
        refreshInstance();
      });
  }

  function wireDaemonActions() {
    ["start", "stop", "restart"].forEach(function (action) {
      var btn = document.getElementById("daemon-" + action);
      if (btn) {
        btn.addEventListener("click", function () {
          daemonAction(action);
        });
      }
    });
  }

  /* ------------------------------------------------------------------ */
  /* Instance page: non-backlog tabs                                     */
  /* ------------------------------------------------------------------ */

  function outcomeBadge(outcome) {
    if (outcome === "SUCCESS") return "COMPLETED";
    if (outcome === "BLOCKED") return "BLOCKED";
    if (outcome === "ERROR" || outcome === "DIRTY") return "FAILED";
    return "OPEN";
  }

  function formatDuration(seconds) {
    if (seconds === null || seconds === undefined || isNaN(seconds)) return "—";
    var total = Math.max(0, Math.round(seconds));
    if (total < 60) return total + "s";
    var minutes = Math.floor(total / 60);
    var rest = total % 60;
    return rest ? minutes + "m " + rest + "s" : minutes + "m";
  }

  function shortSha(sha) {
    if (!sha) return "—";
    return sha.length > 8 ? sha.slice(0, 7) : sha;
  }

  function historyEmptyState() {
    var empty = el("div", "empty-state");
    empty.appendChild(el("p", "empty-title", "No runs yet"));
    empty.appendChild(
      el(
        "p",
        "empty-sub",
        "Finished forgeo cycles appear here, newest first. Each cycle writes one record to runs.jsonl."
      )
    );
    return empty;
  }

  function renderHistory(data) {
    var body = document.getElementById("history-body");
    if (!body) return;
    body.textContent = "";
    var runs = (data && data.runs) || [];
    var total = data && typeof data.total === "number" ? data.total : runs.length;
    if (total === 0) {
      runsPage = 0;
      body.appendChild(historyEmptyState());
      return;
    }
    var pages = Math.max(1, Math.ceil(total / RUNS_PAGE_SIZE));
    if (runsPage > pages - 1) runsPage = pages - 1;

    var table = el("table", "run-table");
    var thead = el("thead");
    var headRow = el("tr");
    ["time", "kind", "task", "outcome", "duration", "commit", "retry", "reason"].forEach(function (h) {
      headRow.appendChild(el("th", null, h));
    });
    thead.appendChild(headRow);
    table.appendChild(thead);

    var tbody = el("tbody");
    runs.forEach(function (run) {
      var row = el("tr");
      row.appendChild(el("td", null, formatTime(run.finished_at)));
      row.appendChild(el("td", null, run.kind || "—"));

      var task = el("td", "run-task");
      task.appendChild(el("span", "run-task-id", run.task_id || "—"));
      if (run.task_title) {
        task.appendChild(document.createTextNode(" "));
        task.appendChild(el("span", "run-task-title", run.task_title));
      }
      row.appendChild(task);

      row.appendChild(el("td", "badge badge--" + outcomeBadge(run.outcome), run.outcome));
      row.appendChild(el("td", "mono", formatDuration(run.duration_seconds)));
      var commit = el("td", "mono", shortSha(run.commit_sha));
      if (run.commit_sha) commit.title = run.commit_sha;
      row.appendChild(commit);
      var retry = el("td", "mono", run.retry_count ? "retry " + run.retry_count : "—");
      if (run.retry_count) retry.title = "run was a retry (retry count at the time)";
      row.appendChild(retry);
      row.appendChild(el("td", run.reason ? "run-reason" : null, run.reason || "—"));
      tbody.appendChild(row);

      if (run.output_logs && run.output_logs.length > 0) {
        var outputRow = el("tr", "run-output-row");
        var outputCell = el("td", null);
        outputCell.colSpan = 8;
        var details = el("details", "run-output");
        var summary = el("summary", "run-output__summary");
        summary.appendChild(el("span", null, "agent output"));
        summary.appendChild(
          el("span", "run-output__count", run.output_logs.length + " lines")
        );
        details.appendChild(summary);
        var pre = el("pre", "run-output__pre");
        pre.textContent = run.output_logs.join("\n");
        details.appendChild(pre);
        outputCell.appendChild(details);
        outputRow.appendChild(outputCell);
        tbody.appendChild(outputRow);
      }
    });
    table.appendChild(tbody);
    body.appendChild(table);

    var pager = el("div", "run-pager");
    var prevBtn = el("button", "run-pager__btn", "← Newer");
    prevBtn.setAttribute("type", "button");
    prevBtn.disabled = runsPage === 0;
    prevBtn.addEventListener("click", function () {
      if (runsPage > 0) {
        runsPage -= 1;
        loadRuns();
      }
    });
    var info = el(
      "span",
      "run-pager__info",
      "page " + (runsPage + 1) + " of " + pages + " · " + total + (total === 1 ? " run" : " runs")
    );
    var nextBtn = el("button", "run-pager__btn", "Older →");
    nextBtn.setAttribute("type", "button");
    nextBtn.disabled = runsPage >= pages - 1;
    nextBtn.addEventListener("click", function () {
      if (runsPage < pages - 1) {
        runsPage += 1;
        loadRuns();
      }
    });
    pager.appendChild(prevBtn);
    pager.appendChild(info);
    pager.appendChild(nextBtn);
    body.appendChild(pager);
  }

  function loadRuns() {
    if (!API) return;
    var url = API + "runs?limit=" + RUNS_PAGE_SIZE + "&offset=" + runsPage * RUNS_PAGE_SIZE;
    fetchJSON(url)
      .then(function (data) {
        renderHistory(data);
        setDown(false);
      })
      .catch(function () {
        setDown(true);
      });
  }

  function renderTextPanel(id, text, fallback) {
    var node = document.getElementById(id);
    if (node) node.textContent = text || fallback;
  }

  /* ------------------------------------------------------------------ */
  /* Config tab: editable forgeo.yaml form                               */
  /* ------------------------------------------------------------------ */

  var CONFIG_FIELDS = [
    { key: "repo", label: "Repository", type: "text", hint: "Path of the git repository Forgeo works on." },
    { key: "interval_minutes", label: "Interval (minutes)", type: "number", min: 1, step: 1 },
    { key: "branch", label: "Branch", type: "text", hint: "Branch everything is committed to (default main)." },
    { key: "remote", label: "Git remote", type: "text", optional: true, hint: "Remote to push to (e.g. origin). Empty = commit locally only." },
    { key: "agent_command", label: "Agent command", type: "textarea", rows: 3, hint: "Shell command (or argv list) that runs the coding agent. The task arrives as the $FORGEO_TASK environment variable; exit 0 = success, blocked_exit_code = needs human input." },
    { key: "agent_timeout_seconds", label: "Agent timeout (seconds)", type: "number", min: 0.1, step: "any", optional: true, hint: "Kill the agent after this many seconds. Empty = never." },
    { key: "blocked_exit_code", label: "Blocked exit code", type: "number", min: 1, step: 1 },
    { key: "no_changes_exit_code", label: "No-changes exit code", type: "number", min: 1, step: 1, hint: "Exit code the agent uses to signal a task needs no code change. Exiting 0 with an unchanged tree fails the task instead." },
    { key: "git_timeout_seconds", label: "Git timeout (seconds)", type: "number", min: 0.1, step: "any" },
    { key: "refactor_prompt", label: "Refactor prompt", type: "textarea", rows: 4, hint: "Instruction used for the refactoring run when the backlog is empty." },
    { key: "backlog", label: "Backlog file", type: "text", hint: "Path of the JSON backlog file (created on first use)." },
    { key: "blocker_file", label: "Blocker file", type: "text", hint: "Where BLOCKER.md is written when the agent needs human input. Keep it outside the repository." },
    { key: "log_file", label: "Log file", type: "text" },
    { key: "run_output_lines", label: "Run output lines", type: "number", min: 0, step: 1, hint: "How many agent output lines each run record keeps in runs.jsonl (bounded tail, shown in the History tab). 0 = don't persist agent output." },
    { key: "failed_retry_max", label: "Failed retry max", type: "number", min: 0, step: 1, hint: "How many times a FAILED task is retried automatically. 0 = disabled (a FAILED task stays FAILED until a human reopens it). A task can override this with its own retries_left." },
    { key: "failed_retry_wait_cycles", label: "Failed retry wait (cycles)", type: "number", min: 1, step: 1, hint: "How many cycles a FAILED task waits (backoff) before it is retried." },
    { key: "agent_sandbox", label: "Agent sandbox", type: "select", options: ["none", "docker"], hint: "none = run directly on the host; docker = run inside a container." },
    { key: "agent_sandbox_image", label: "Sandbox image", type: "text", optional: true, hint: "Container image used when agent_sandbox is docker. Required in that mode." },
    { key: "agent_sandbox_network", label: "Sandbox network", type: "text", hint: "Docker network for the sandboxed agent (--network). Default none = networking disabled." },
    { key: "agent_sandbox_mounts", label: "Sandbox mounts", type: "textarea", rows: 2, optional: true, hint: "Host paths mounted read-only into the container, one per line." },
    { key: "agent_env", label: "Agent environment", type: "textarea", rows: 3, optional: true, hint: "Extra environment variables for the agent process, one KEY=VALUE per line." },
    { key: "telegram_chat_id", label: "Telegram chat id", type: "text", optional: true },
    { key: "telegram_bot_token", label: "Telegram bot token", type: "readonly", hint: "Protected: not editable through the web console." }
  ];

  function configValue(field, config) {
    var v = config ? config[field.key] : "";
    if (v === undefined || v === null) return "";
    if (field.key === "telegram_bot_token") {
      var secret = String(v);
      return secret.length > 4 ? "••••••••" + secret.slice(-4) : "••••••••";
    }
    if (field.key === "agent_env" && typeof v === "object" && !Array.isArray(v)) {
      return Object.keys(v)
        .map(function (key) {
          return key + "=" + v[key];
        })
        .join("\n");
    }
    if (Array.isArray(v)) return field.key === "agent_command" ? v.join(" ") : v.join("\n");
    return String(v);
  }

  function markConfigDirty() {
    configDirty = true;
    var status = document.getElementById("config-save-status");
    if (status && status.dataset.state === "saved") {
      status.textContent = "unsaved changes";
      status.dataset.state = "dirty";
    }
  }

  function buildConfigField(field, config) {
    var wrap = el("label", "config-field");
    wrap.setAttribute("data-config-key", field.key);
    wrap.appendChild(el("span", "config-field__label", field.label));

    var control;
    var value = configValue(field, config);
    if (field.type === "readonly") {
      control = el("span", "config-field__readonly", value);
    } else if (field.type === "textarea") {
      control = document.createElement("textarea");
      control.id = "config-" + field.key;
      control.rows = field.rows || 3;
      control.value = value;
    } else if (field.type === "select") {
      control = document.createElement("select");
      control.id = "config-" + field.key;
      (field.options || []).forEach(function (optionValue) {
        var option = document.createElement("option");
        option.value = optionValue;
        option.textContent = optionValue;
        if (optionValue === value) option.selected = true;
        control.appendChild(option);
      });
    } else {
      control = document.createElement("input");
      control.id = "config-" + field.key;
      control.type = field.type === "number" ? "number" : "text";
      if (field.type === "number") {
        if (field.min !== undefined) control.min = String(field.min);
        if (field.step !== undefined) control.step = String(field.step);
      }
      control.value = value;
    }
    if (field.type !== "readonly") {
      control.addEventListener("input", markConfigDirty);
      control.addEventListener("change", markConfigDirty);
    }
    wrap.appendChild(control);

    var error = el("span", "config-field__error");
    error.hidden = true;
    wrap.appendChild(error);

    if (field.hint) wrap.appendChild(el("span", "config-field__hint", field.hint));
    return wrap;
  }

  function setConfigLoading(show) {
    var loading = document.getElementById("config-loading");
    if (loading) loading.hidden = !show;
  }

  function showConfigError(message) {
    setConfigLoading(false);
    var form = document.getElementById("config-form");
    if (form) form.hidden = true;
    var hint = document.getElementById("config-reload-hint");
    if (hint) hint.hidden = true;
    var error = document.getElementById("config-error");
    if (error) {
      error.textContent = message || "config could not be loaded";
      error.hidden = false;
    }
  }

  function renderConfig(config) {
    setConfigLoading(false);
    configFormBuilt = true;
    configDirty = false;
    var error = document.getElementById("config-error");
    if (error) error.hidden = true;
    var hint = document.getElementById("config-reload-hint");
    if (hint) hint.hidden = true;
    var form = document.getElementById("config-form");
    var fields = document.getElementById("config-fields");
    if (!form || !fields) return;
    fields.textContent = "";
    CONFIG_FIELDS.forEach(function (field) {
      fields.appendChild(buildConfigField(field, config));
    });
    form.hidden = false;
    var status = document.getElementById("config-save-status");
    if (status) {
      status.textContent = "";
      status.dataset.state = "";
    }
    var saveBtn = document.getElementById("config-save");
    if (saveBtn) saveBtn.disabled = false;
  }

  function clearConfigFieldErrors() {
    var wrappers = document.querySelectorAll(".config-field--invalid");
    for (var i = 0; i < wrappers.length; i++) {
      wrappers[i].classList.remove("config-field--invalid");
      var error = wrappers[i].querySelector(".config-field__error");
      if (error) {
        error.hidden = true;
        error.textContent = "";
      }
    }
  }

  function highlightConfigErrors(message) {
    clearConfigFieldErrors();
    var text = String(message || "");
    var prefix = "invalid config: ";
    var body = text.indexOf(prefix) === 0 ? text.slice(prefix.length) : text;
    body.split("; ").forEach(function (segment) {
      var idx = segment.indexOf(":");
      if (idx < 0) return;
      var loc = segment.slice(0, idx).trim();
      var detail = segment.slice(idx + 1).trim();
      var fieldKey = loc.split(".")[0];
      var wrapper = document.querySelector('[data-config-key="' + fieldKey + '"]');
      if (!wrapper) return;
      wrapper.classList.add("config-field--invalid");
      var error = wrapper.querySelector(".config-field__error");
      if (error) {
        error.textContent = detail;
        error.hidden = false;
      }
    });
  }

  function collectConfig() {
    var payload = { name: instanceName };
    CONFIG_FIELDS.forEach(function (field) {
      var node = document.getElementById("config-" + field.key);
      if (!node) return;
      var raw = node.value;
      if (field.key === "agent_env") {
        var env = {};
        String(raw)
          .split(/\r?\n/)
          .forEach(function (line) {
            line = line.trim();
            if (!line) return;
            var eq = line.indexOf("=");
            if (eq < 0) env[line] = "";
            else env[line.slice(0, eq).trim()] = line.slice(eq + 1).trim();
          });
        payload[field.key] = env;
      } else if (field.key === "agent_sandbox_mounts") {
        payload[field.key] = splitLines(raw);
      } else if (field.type === "number") {
        var trimmed = String(raw).trim();
        if (trimmed === "") {
          payload[field.key] = field.optional ? null : Number("0");
        } else {
          payload[field.key] = Number(trimmed);
        }
      } else {
        var value = String(raw).trim();
        payload[field.key] = field.optional && value === "" ? null : value;
      }
    });
    return payload;
  }

  function renderReloadHint(message, status) {
    var hint = document.getElementById("config-reload-hint");
    if (!hint) return;
    hint.textContent = "";
    hint.appendChild(el("strong", null, "Config saved"));
    var notice = String(message || "The daemon picks up changes on its next cycle.").replace(/^Config saved\.?\s*/i, "");
    hint.appendChild(document.createTextNode(" — " + notice + " "));
    if (status) {
      var state = status.daemon_running ? "running" : "stopped";
      hint.appendChild(
        document.createTextNode(
          state === "running"
            ? "The daemon is currently running and picks up the change on its next cycle."
            : "The daemon is currently stopped; the change applies on its next start."
        )
      );
    } else {
      hint.appendChild(
        document.createTextNode("A running daemon picks up the change on its next cycle.")
      );
    }
    hint.hidden = false;
  }

  function saveConfig() {
    if (!API) return;
    var error = document.getElementById("config-error");
    var status = document.getElementById("config-save-status");
    var saveBtn = document.getElementById("config-save");
    if (error) error.hidden = true;
    clearConfigFieldErrors();
    if (saveBtn) saveBtn.disabled = true;
    if (status) {
      status.textContent = "saving…";
      status.dataset.state = "saving";
    }

    apiFetch(API + "config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(collectConfig()),
    })
      .then(function (resp) {
        if (!resp.ok) {
          return resp.json().then(function (data) {
            throw new Error((data && data.error) || "HTTP " + resp.status);
          });
        }
        return resp.json();
      })
      .then(function (data) {
        renderConfig(data.config);
        if (status) {
          status.textContent = "saved";
          status.dataset.state = "saved";
        }
        return fetchJSON(API + "status")
          .then(function (statusData) {
            instanceStatus = statusData || {};
            return statusData;
          })
          .catch(function () {
            return null;
          })
          .then(function (statusData) {
            renderReloadHint(data.message, statusData);
          });
      })
      .catch(function (err) {
        if (saveBtn) saveBtn.disabled = false;
        if (status) {
          status.textContent = "";
          status.dataset.state = "";
        }
        highlightConfigErrors(err.message);
        if (error) {
          error.textContent = err.message || "failed to save config";
          error.hidden = false;
        }
      });
  }

  function loadConfig() {
    if (!API) return;
    if (configDirty) return;
    if (!configFormBuilt) setConfigLoading(true);
    fetchJSON(API + "config")
      .then(function (config) {
        if (configDirty) return;
        if (config && typeof config.error === "string") {
          showConfigError(config.error);
          return;
        }
        renderConfig(config);
        setDown(false);
      })
      .catch(function (err) {
        setDown(true);
        showConfigError(err && err.message ? err.message : "config could not be loaded");
      });
  }

  function loadTab(tab) {
    if (!API || tab === "backlog" || tab === "create") return;
    if (tab === "logs") {
      fetchJSON(API + "logs?lines=200")
        .then(function (data) {
          renderTextPanel("logs-body", (data.lines || []).join("\n"), "(empty log)");
          setDown(false);
        })
        .catch(function () {
          setDown(true);
        });
    } else if (tab === "history") {
      loadRuns();
    } else if (tab === "blocker") {
      fetchJSON(API + "blocker")
        .then(function (data) {
          renderTextPanel("blocker-body", data.content, "(no blocker)");
          setDown(false);
        })
        .catch(function () {
          setDown(true);
        });
    } else if (tab === "config") {
      loadConfig();
    }
  }

  function activate(tab) {
    currentTab = tab;
    TABS.forEach(function (t) {
      var panel = document.getElementById("tab-" + t);
      if (panel) panel.hidden = t !== tab;
    });
    var buttons = document.querySelectorAll(".tab[data-tab]");
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].classList.toggle("is-active", buttons[i].dataset.tab === tab);
    }
    if (tab !== "backlog") loadTab(tab);
  }

  function refreshInstance() {
    if (!API) return;
    Promise.all([fetchJSON(API + "tasks"), fetchJSON(API + "status")])
      .then(function (results) {
        renderTasks(results[0] || []);
        renderStatus(results[1] || {});
        setDown(false);
        stampFetchTime();
      })
      .catch(function () {
        setDown(true);
      });
  }

  function wireNewTask() {
    var form = document.getElementById("new-task");
    var error = document.getElementById("new-task-error");
    if (!form || !API) return;

    var criteria = [];
    var criteriaInput = document.getElementById("task-acceptance-input");
    var criteriaList = document.getElementById("task-acceptance-list");

    function renderCriteria() {
      if (!criteriaList) return;
      criteriaList.textContent = "";
      criteria.forEach(function (criterion, index) {
        var chip = el("li", "new-task__criteria-chip", null);
        chip.appendChild(document.createTextNode(criterion));
        var remove = document.createElement("button");
        remove.type = "button";
        remove.className = "new-task__criteria-remove";
        remove.setAttribute("aria-label", "Remove acceptance criterion");
        remove.textContent = "×";
        remove.addEventListener("click", function () {
          criteria.splice(index, 1);
          renderCriteria();
        });
        chip.appendChild(remove);
        criteriaList.appendChild(chip);
      });
    }

    function addCriterion() {
      if (!criteriaInput) return;
      var value = criteriaInput.value.trim();
      if (!value) return;
      criteria.push(value);
      criteriaInput.value = "";
      criteriaInput.focus();
      renderCriteria();
    }

    var addButton = document.getElementById("task-acceptance-add");
    if (addButton) addButton.addEventListener("click", addCriterion);
    if (criteriaInput) {
      criteriaInput.addEventListener("keydown", function (event) {
        if (event.key === "Enter") {
          event.preventDefault();
          addCriterion();
        }
      });
    }

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (error) error.hidden = true;

      var title = document.getElementById("task-title").value.trim();
      if (!title) {
        if (error) {
          error.textContent = "title is required";
          error.hidden = false;
        }
        return;
      }
      var description = document.getElementById("task-description").value.trim();
      if (!description) {
        if (error) {
          error.textContent = "description is required";
          error.hidden = false;
        }
        return;
      }
      var commandInput = document.getElementById("task-command");
      var command = commandInput ? commandInput.value.trim() : "";

      apiFetch(API + "tasks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: title,
          description: description,
          acceptance_criteria: criteria.slice(),
          agent_command: command ? command : null,
        }),
      })
        .then(function (resp) {
          if (!resp.ok) {
            return resp.json().then(function (data) {
              throw new Error((data && data.error) || "HTTP " + resp.status);
            });
          }
          return resp.json();
        })
        .then(function () {
          form.reset();
          criteria = [];
          renderCriteria();
          return fetchJSON(API + "tasks");
        })
        .then(function (tasks) {
          renderTasks(tasks || []);
        })
        .catch(function (err) {
          if (error) {
            error.textContent = err.message || "failed to add task";
            error.hidden = false;
          }
        });
    });
  }

  function wire() {
    if (page === "instance") {
      readFilters();
      wireBacklogFilters();
      wireNewTask();
      wireDaemonActions();
      var configForm = document.getElementById("config-form");
      if (configForm) {
        configForm.addEventListener("submit", function (event) {
          event.preventDefault();
          saveConfig();
        });
      }
      var buttons = document.querySelectorAll(".tab[data-tab]");
      for (var i = 0; i < buttons.length; i++) {
        buttons[i].addEventListener("click", function () {
          activate(this.dataset.tab);
        });
      }

      var closeBtn = document.getElementById("task-modal-close");
      if (closeBtn) closeBtn.addEventListener("click", closeModal);
      var editBtn = document.getElementById("task-modal-edit");
      if (editBtn) editBtn.addEventListener("click", enterEditMode);
      var reopenBtn = document.getElementById("task-modal-reopen");
      if (reopenBtn) reopenBtn.addEventListener("click", reopenTask);
      var deleteBtn = document.getElementById("task-modal-delete");
      if (deleteBtn) deleteBtn.addEventListener("click", deleteTask);
      var cancelBtn = document.getElementById("task-modal-cancel");
      if (cancelBtn) cancelBtn.addEventListener("click", exitEditMode);
      var editForm = document.getElementById("task-modal-edit-form");
      if (editForm) {
        editForm.addEventListener("submit", function (event) {
          event.preventDefault();
          saveTask();
        });
      }
      var modal = document.getElementById("task-modal");
      if (modal) {
        modal.addEventListener("click", function (event) {
          if (event.target === modal) closeModal();
        });
      }
      document.addEventListener("keydown", function (event) {
        if (event.key === "Escape") closeModal();
      });
    }
  }

  /* ------------------------------------------------------------------ */
  /* Boot                                                                */
  /* ------------------------------------------------------------------ */

  /* Token-URL form: a `?token=...` query signs the browser in and is
     stripped from the URL so it never lingers in the address bar. */
  (function () {
    var params = new URLSearchParams(location.search);
    var urlToken = params.get("token");
    if (urlToken) {
      storeToken(urlToken);
      params.delete("token");
      var search = params.toString();
      var cleanUrl = location.pathname + (search ? "?" + search : "") + location.hash;
      history.replaceState(null, "", cleanUrl);
    }
  })();

  function refresh() {
    if (page === "home") {
      refreshHome();
    } else if (page === "instance") {
      refreshInstance();
      if (currentTab !== "backlog") loadTab(currentTab);
    }
  }

  if (page === "instance") {
    buildColumns();
  }
  wire();
  refresh();
  setInterval(refresh, REFRESH_MS);
})();
