let running = Boolean(window.MEDIA_CLEANUP_RUNNING);
let lastData = null;
let activeView = "overview";
let modalAction = null;
let toastTimer = null;

const filters = { download: "all", duplicate: "all", library: "all", quarantine: "all" };
const searches = { download: "", duplicate: "", library: "", quarantine: "" };
const sorts = { download: "recommended", duplicate: "largest", library: "largest", quarantine: "newest" };
const displayLimits = { download: 100, duplicate: 100, library: 100, quarantine: 100 };
const displayStep = 100;
const selections = {
  download: new Set(),
  duplicate: new Set(),
  library: new Set(),
  quarantine: new Set(),
};

async function runAudit() {
  const button = document.getElementById("run");
  button.disabled = true;
  setStatus(true, "Starting audit");
  try {
    const response = await fetch("/run", { method: "POST" });
    if (!response.ok) throw new Error(`Audit could not start (${response.status})`);
    showMessage("Audit started. Results will appear here when it finishes.", false);
    poll();
  } catch (error) {
    button.disabled = false;
    setStatus(false, "Ready");
    showMessage(error.message || String(error), true);
  }
}

async function poll() {
  try {
    const response = await fetch("/data");
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.error || `Dashboard data failed (${response.status})`);
    }
    const data = await response.json();
    render(data);
    if (data.status && data.status.running) setTimeout(poll, 1500);
  } catch (error) {
    document.getElementById("run").disabled = false;
    showMessage(error.message || String(error), true);
  }
}

function render(data) {
  lastData = data;
  const status = data.status || {};
  document.getElementById("run").disabled = Boolean(status.running);
  const statusText = status.running ? "Running audit" : "Ready";
  const timeText = status.last_finished
    ? `Last finished ${formatDate(status.last_finished)}`
    : data.generated_at
      ? `Last scan ${formatDate(data.generated_at)}`
      : status.last_started
        ? `Started ${formatDate(status.last_started)}`
        : "No audit yet";
  setStatus(Boolean(status.running), statusText, timeText);
  if (status.last_error) showMessage(status.last_error, true);

  renderOverview(data);
  renderStorageVolumes(data.storage_volumes || []);
  renderScanned(data.scan_breakdown || []);
  renderDownloads(data.download_candidates || [], data.download_summary || {});
  renderCandidates("duplicate", data.duplicate_candidates || []);
  renderCandidates("library", data.library_review || data.safe_candidates || []);
  renderQuarantine(data.quarantined || { rows: [], empty: true, items: 0, recoverable_size: "0 B" });
  renderLinks(data.reports || {});
  renderNavCounts(data);
}

function renderStorageVolumes(volumes) {
  const target = document.getElementById("storageVolumes");
  if (!target) return;
  target.innerHTML = volumes.length ? volumes.map((volume) => {
    const percent = Math.max(0, Math.min(100, Number(volume.used_percent || 0)));
    const tone = percent >= 90 ? "danger" : percent >= 80 ? "attention" : "";
    return `<div class="volume-card ${tone}">
      <div class="volume-label">${escapeHtml(volume.label || "Storage")}</div>
      <div class="volume-value">${escapeHtml(volume.free_human || "0 B")} free</div>
      <div class="volume-detail">${escapeHtml(volume.used_human || "0 B")} used of ${escapeHtml(volume.total_human || "0 B")}</div>
      <div class="volume-track" aria-label="${percent}% used"><span style="width:${percent}%"></span></div>
      <div class="volume-percent">${percent}% used</div>
    </div>`;
  }).join("") : `<div class="empty-state">Mounted storage details are unavailable.</div>`;
}

function renderOverview(data) {
  const latest = data.latest || { files_scanned: 0, safe_count: 0, reclaimable: "0 B" };
  const downloads = data.download_summary || {};
  const duplicates = data.duplicate_candidates || [];
  const library = data.library_review || data.safe_candidates || [];
  const quarantine = data.quarantined || { items: 0, recoverable_size: "0 B" };
  const protections = data.protections || {};
  const hasAudit = Number(latest.files_scanned || 0) > 0 || (data.scan_breakdown || []).length > 0;
  let focus = {
    eyebrow: "Next best action",
    title: "Ready for your first audit",
    body: "Scan the media folders to build a cleanup review.",
    button: "Run audit",
    action: "audit",
  };

  if (data.status && data.status.running) {
    focus = {
      eyebrow: "Audit in progress",
      title: "Scanning your media",
      body: "Results will organize themselves as soon as the scan finishes.",
      button: "Scanning",
      action: "none",
    };
  } else if (Number(downloads.high_confidence || 0) > 0) {
    focus = {
      eyebrow: "Best cleanup opportunity",
      title: `${downloads.high_confidence} likely download ${plural(downloads.high_confidence, "copy", "copies")}`,
      body: `${downloads.likely_reclaimable || "Unknown space"} has an exact title and episode/year match in the library.`,
      button: "Review downloads",
      action: "downloads",
    };
  } else if (duplicates.length) {
    focus = {
      eyebrow: "Ready to compare",
      title: `${duplicates.length} larger ${plural(duplicates.length, "version", "versions")} found`,
      body: `${latest.reclaimable || "Unknown space"} can move to quarantine after a side-by-side review.`,
      button: "Review duplicates",
      action: "duplicates",
    };
  } else if (library.length) {
    focus = {
      eyebrow: "Needs a decision",
      title: `${library.length} untracked library ${plural(library.length, "file", "files")}`,
      body: "These video files were found in library folders but were not confirmed by the media apps.",
      button: "Review files",
      action: "library",
    };
  } else if (hasAudit) {
    focus = {
      eyebrow: "Latest audit",
      title: "Everything looks tidy",
      body: `${latest.files_scanned || 0} video files scanned with no immediate cleanup recommendation.`,
      button: "Run again",
      action: "audit",
    };
  }

  document.getElementById("focusEyebrow").textContent = focus.eyebrow;
  document.getElementById("focusTitle").textContent = focus.title;
  document.getElementById("focusBody").textContent = focus.body;
  const focusButton = document.getElementById("focusButton");
  focusButton.textContent = focus.button;
  focusButton.disabled = focus.action === "none";
  focusButton.onclick = focus.action === "audit" ? runAudit : () => goTo(focus.action);

  document.getElementById("overviewTimestamp").textContent = data.generated_at
    ? `Last scan ${formatDate(data.generated_at)} · ${latest.files_scanned || 0} video files`
    : "Waiting for the first audit.";
  const qbitUnavailable = hasAudit && !protections.qbittorrent_enabled;
  document.getElementById("qbitBanner").classList.toggle("show", qbitUnavailable);
  if (qbitUnavailable) {
    const failed = Boolean(protections.qbittorrent_error);
    document.getElementById("qbitTitle").textContent = failed ? "Seeding check unavailable" : "Seeding check is off";
    document.getElementById("qbitBody").textContent = failed
      ? "qBittorrent could not be reached or signed in to. Downloads require a manual seeding check before quarantine."
      : "Download matches need a quick seeding check before quarantine.";
  }
  renderStorageSafety(data.storage_safety || {});

  const cards = [
    {
      label: "Likely download copies",
      value: downloads.high_confidence || 0,
      sub: downloads.likely_reclaimable || "0 B",
      attention: Number(downloads.high_confidence || 0) > 0,
    },
    { label: "Confirmed duplicates", value: duplicates.length, sub: latest.reclaimable || "0 B", attention: false },
    { label: "Library review", value: library.length, sub: "Needs your decision", attention: false },
    { label: "In quarantine", value: quarantine.items || 0, sub: quarantine.recoverable_size || "0 B", attention: false },
  ];
  document.getElementById("overviewCards").innerHTML = cards.map((card) => `
    <div class="summary-card ${card.attention ? "attention" : ""}">
      <div class="eyebrow">${escapeHtml(card.label)}</div>
      <div class="value">${escapeHtml(card.value)}</div>
      <div class="sub">${escapeHtml(card.sub)}</div>
    </div>`).join("");

  const actions = [
    {
      view: "downloads",
      title: "Review Downloads",
      body: `${downloads.items || 0} files · ${downloads.total_size || "0 B"}`,
      value: downloads.high_confidence ? `${downloads.high_confidence} likely copies` : "Review",
    },
    {
      view: "duplicates",
      title: "Compare Duplicates",
      body: "Larger file beside the version that stays",
      value: `${duplicates.length} items`,
    },
    {
      view: "library",
      title: "Check Library Files",
      body: "Untracked video files inside Movies, TV, or Anime",
      value: `${library.length} items`,
    },
    {
      view: "quarantine",
      title: "Open Quarantine",
      body: "Restore or permanently delete verified files",
      value: `${quarantine.items || 0} items`,
    },
  ];
  document.getElementById("nextActions").innerHTML = actions.map((action) => `
    <button class="action-row" onclick="goTo('${action.view}')">
      <span><strong>${escapeHtml(action.title)}</strong><span class="path">${escapeHtml(action.body)}</span></span>
      <span class="action-value">${escapeHtml(action.value)}</span>
    </button>`).join("");
}

function renderStorageSafety(storage) {
  const banner = document.getElementById("storageBanner");
  if (!banner) return;
  const ready = Boolean(storage.ready);
  banner.classList.toggle("attention", !ready);
  document.getElementById("storageTitle").textContent = ready ? "NAS quarantine ready" : "Storage check needed";
  document.getElementById("storageBody").textContent = storage.message || "Checking storage safety...";
}

function renderScanned(rows) {
  const visibleRows = rows.filter((row) => row.location !== "other" || row.file_count);
  const total = visibleRows.reduce((sum, row) => sum + Number(row.file_count || 0), 0);
  document.getElementById("scanTotal").textContent = `${total} files`;
  document.getElementById("scanned").innerHTML = visibleRows.length
    ? visibleRows.map((row) => `
      <div class="scan-source">
        <div class="eyebrow">${escapeHtml(titleCase(row.location))}</div>
        <strong>${row.file_count} files</strong>
        <div class="path">${escapeHtml(row.total_size || "0 B")} · ${escapeHtml(row.root || "Other locations")}</div>
      </div>`).join("")
    : emptyHtml("Run an audit to see scanned folders.");
}

function renderCandidates(kind, rows) {
  const filtered = filteredRows(kind, rows);
  const visible = visibleRows(kind, filtered);
  const target = kind === "duplicate" ? "duplicates" : "libraryRows";
  const summary = kind === "duplicate" ? "duplicateSummary" : "librarySummary";
  renderQueueBrief(kind, rows, filtered, visible);
  document.getElementById(summary).innerHTML = `<span><strong>${rows.length}</strong> total</span><span><strong>${visible.length}</strong> shown</span>`;
  document.getElementById(target).innerHTML = filtered.length
    ? visible.map((row) => candidateRowHtml(kind, row)).join("") + loadMoreHtml(kind, filtered, visible)
    : emptyHtml(searches[kind] || filters[kind] !== "all"
      ? "No files match this view."
      : kind === "duplicate" ? "No duplicate candidates." : "No untracked library files.");
  restoreSelection(kind);
  renderSelectionBar(kind);
}

function candidateRowHtml(kind, row) {
  const selected = selections[kind].has(String(row.path));
  const type = kind === "duplicate" ? titleCase(row.kind || "media") : titleCase(row.location || row.kind || "library");
  const note = recommendationText(kind, row);
  return `<label class="media-row ${selected ? "selected" : ""}">
    <input type="checkbox" data-kind="${kind}" value="${escapeAttr(row.path)}" ${selected ? "checked" : ""} onchange="syncSelection(this)">
    <div>
      <div class="row-top"><div class="item-title">${escapeHtml(row.title || fileName(row.path))}</div><div class="row-size">${escapeHtml(row.size_human || "Unknown")}</div></div>
      ${row.keeper ? compareHtml(row) : `<div class="path">${escapeHtml(row.path)}</div>`}
      <div class="row-reason">${escapeHtml(note)}</div>
      <div class="tags"><span class="tag ${kind === "duplicate" ? "high" : "review"}">${kind === "duplicate" ? "Confirmed match" : "Needs review"}</span><span class="tag">${escapeHtml(type)}</span></div>
    </div>
  </label>`;
}

function compareHtml(row) {
  return `<div class="compare">
    <div class="compare-side remove">
      <div class="compare-label">Move to quarantine</div>
      <div class="compare-file">${escapeHtml(fileName(row.path))}</div>
      <div class="compare-size">${escapeHtml(row.size_human || "Unknown")}</div>
      <div class="path">${escapeHtml(parentPath(row.path))}</div>
    </div>
    <div class="compare-side keep">
      <div class="compare-label">Keep in library</div>
      <div class="compare-file">${escapeHtml(fileName(row.keeper))}</div>
      <div class="compare-size">${escapeHtml(row.keeper_size_human || "Unknown")}</div>
      <div class="path">${escapeHtml(parentPath(row.keeper))}</div>
    </div>
  </div>`;
}

function renderDownloads(rows, summary) {
  const filtered = filteredRows("download", rows);
  const visible = visibleRows("download", filtered);
  renderQueueBrief("download", rows, filtered, visible, summary);
  document.getElementById("downloadSummary").innerHTML = `
    <span><strong>${summary.items || 0}</strong> files</span>
    <span><strong>${escapeHtml(summary.total_size || "0 B")}</strong> total</span>
    <span><strong>${summary.high_confidence || 0}</strong> library matches</span>
    <span><strong>${summary.older_than_14_days || 0}</strong> 14+ days old</span>
    <span><strong>${visible.length}</strong> shown</span>`;
  document.getElementById("downloads").innerHTML = filtered.length
    ? visible.map((row) => {
      const selected = selections.download.has(String(row.path));
      const note = recommendationText("download", row);
      return `<label class="media-row ${selected ? "selected" : ""}">
        <input type="checkbox" data-kind="download" value="${escapeAttr(row.path)}" ${selected ? "checked" : ""} onchange="syncSelection(this)">
        <div>
          <div class="row-top"><div class="item-title">${escapeHtml(row.title || fileName(row.path))}</div><div class="row-size">${escapeHtml(row.size_human || "Unknown")}</div></div>
          ${row.keeper ? compareHtml(row) : `<div class="path">${escapeHtml(row.path)}</div>`}
          <div class="row-reason">${escapeHtml(note)}</div>
          <div class="tags">
            <span class="tag ${String(row.confidence || "review").toLowerCase()}">${row.confidence === "High" ? "Library match" : escapeHtml(row.confidence || "Review")}</span>
            <span class="tag">${escapeHtml(row.bucket || "Download")}</span>
            <span class="tag">${row.age_days === "" ? "Age unknown" : `${row.age_days} days old`}</span>
          </div>
        </div>
      </label>`;
    }).join("") + loadMoreHtml("download", filtered, visible)
    : emptyHtml(searches.download || filters.download !== "all" ? "No downloads match this view." : "No download cleanup rows.");
  restoreSelection("download");
  renderSelectionBar("download");
}

function renderQuarantine(quarantine) {
  const rows = quarantine.rows || [];
  const filtered = filteredRows("quarantine", rows);
  const visible = visibleRows("quarantine", filtered);
  renderQueueBrief("quarantine", rows, filtered, visible, quarantine);
  document.getElementById("quarantineSummary").innerHTML = `<span><strong>${quarantine.items || 0}</strong> items</span><span><strong>${escapeHtml(quarantine.recoverable_size || "0 B")}</strong> recoverable</span><span><strong>${visible.length}</strong> shown</span>`;
  document.getElementById("quarantined").innerHTML = filtered.length
    ? visible.map((row) => {
      const selected = selections.quarantine.has(String(row.id));
      return `<label class="media-row ${selected ? "selected" : ""}">
        <input type="checkbox" data-kind="quarantine" value="${escapeAttr(row.id)}" ${selected ? "checked" : ""} onchange="syncSelection(this)">
        <div>
          <div class="row-top"><div class="item-title">${escapeHtml(row.title || fileName(row.original_path))}</div><div class="row-size">${escapeHtml(row.size_human || "Unknown")}</div></div>
          <div class="path">From: ${escapeHtml(row.original_path || "")}</div>
          <div class="tags"><span class="tag">Moved ${escapeHtml(formatDate(row.moved_at || ""))}</span><span class="tag high">Recoverable</span></div>
        </div>
      </label>`;
    }).join("") + loadMoreHtml("quarantine", filtered, visible)
    : emptyHtml(searches.quarantine ? "No quarantined files match your search." : "Quarantine is empty.");
  restoreSelection("quarantine");
  renderSelectionBar("quarantine");
}

function renderLinks(reports) {
  if (!reports.raw_json) {
    document.getElementById("links").innerHTML = `<span class="sub">Reports appear after the first audit.</span>`;
    return;
  }
  document.getElementById("links").innerHTML = `
    <a class="link" href="/reports/${encodeURIComponent(reports.html_report)}" target="_blank">View report</a>
    <a class="link" href="/reports/${encodeURIComponent(reports.details_csv)}">Download details</a>`;
}

function getRows(kind) {
  if (!lastData) return [];
  if (kind === "download") return lastData.download_candidates || [];
  if (kind === "duplicate") return lastData.duplicate_candidates || [];
  if (kind === "library") return lastData.library_review || lastData.safe_candidates || [];
  if (kind === "quarantine") return (lastData.quarantined || {}).rows || [];
  return [];
}

function filteredRows(kind, suppliedRows) {
  let rows = [...(suppliedRows || getRows(kind))];
  const filter = filters[kind];
  const search = searches[kind].trim().toLowerCase();

  if (kind === "download") {
    if (filter === "high") rows = rows.filter((row) => row.confidence === "High");
    if (filter === "old") rows = rows.filter((row) => Number(row.age_days) >= 14);
    if (filter === "review") rows = rows.filter((row) => row.confidence !== "High");
  } else if (kind === "duplicate" && filter !== "all") {
    rows = rows.filter((row) => row.kind === filter);
  } else if (kind === "library" && filter !== "all") {
    rows = rows.filter((row) => row.location === filter);
  }

  if (search) {
    rows = rows.filter((row) => [row.title, row.path, row.original_path, row.keeper, row.bucket, row.folder]
      .some((value) => String(value || "").toLowerCase().includes(search)));
  }

  const sort = sorts[kind];
  if (sort === "largest") rows.sort((left, right) => Number(right.size || 0) - Number(left.size || 0));
  if (sort === "oldest") rows.sort((left, right) => Number(right.age_days || -1) - Number(left.age_days || -1));
  if (sort === "name") rows.sort((left, right) => String(left.title || left.path || "").localeCompare(String(right.title || right.path || "")));
  if (sort === "newest") rows.sort((left, right) => String(right.moved_at || "").localeCompare(String(left.moved_at || "")));
  return rows;
}

function renderQueueBrief(kind, rows, filtered, visible, summary = {}) {
  const target = document.getElementById(`${kind}Brief`);
  if (!target) return;
  const total = rows.length;
  const shown = visible.length;
  const selected = selections[kind] ? selections[kind].size : 0;
  let title = "Review queue";
  let body = `${shown} of ${filtered.length} shown. ${selected} selected.`;
  let tone = "";

  if (kind === "download") {
    const high = Number(summary.high_confidence || rows.filter((row) => row.confidence === "High").length);
    const old = Number(summary.older_than_14_days || rows.filter((row) => Number(row.age_days) >= 14).length);
    title = high ? "Start with library matches" : "Sort the downloads pile";
    body = high
      ? `${high} exact library ${plural(high, "match", "matches")} found. Select matches only moves the visible exact matches.`
      : `${old} files are 14+ days old. Without qBittorrent protection, treat old downloads as review items.`;
    tone = high ? "good" : "warn";
  } else if (kind === "duplicate") {
    title = total ? "Compare before quarantine" : "No duplicate versions";
    body = total
      ? "Each row pairs the file to move with the library file that stays."
      : "No larger duplicate candidates are waiting in this scan.";
    tone = total ? "good" : "";
  } else if (kind === "library") {
    title = total ? "Possible library leftovers" : "No library leftovers";
    body = total
      ? "These videos live in a library folder but were not confirmed by the media apps."
      : "Movies, TV, and Anime do not have unconfirmed video files in this scan.";
    tone = total ? "warn" : "good";
  } else if (kind === "quarantine") {
    title = total ? "Recoverable holding area" : "Quarantine is empty";
    body = total
      ? `${summary.recoverable_size || "0 B"} is recoverable until you type DELETE on the permanent delete screen.`
      : "Nothing is waiting for restore or permanent delete.";
    tone = total ? "" : "good";
  }

  target.className = `queue-brief ${tone}`;
  target.innerHTML = `<strong>${escapeHtml(title)}</strong><span>${escapeHtml(body)}</span>`;
}

function visibleRows(kind, rows) {
  return rows.slice(0, displayLimits[kind] || displayStep);
}

function loadMoreHtml(kind, filtered, visible) {
  if (visible.length >= filtered.length) return "";
  return `<div class="load-more">
    <span>Showing ${visible.length} of ${filtered.length}</span>
    <button onclick="showMore('${kind}')">Show ${Math.min(displayStep, filtered.length - visible.length)} more</button>
  </div>`;
}

function showMore(kind) {
  displayLimits[kind] = (displayLimits[kind] || displayStep) + displayStep;
  rerenderKind(kind);
}

function resetDisplayLimit(kind) {
  displayLimits[kind] = displayStep;
}

function setFilter(kind, value, button) {
  filters[kind] = value;
  resetDisplayLimit(kind);
  document.querySelectorAll(`[data-segments="${kind}"] button`).forEach((item) => item.classList.toggle("active", item === button));
  rerenderKind(kind);
}

function setSearch(kind, value) {
  searches[kind] = value;
  resetDisplayLimit(kind);
  rerenderKind(kind);
}

function setSort(kind, value) {
  sorts[kind] = value;
  resetDisplayLimit(kind);
  rerenderKind(kind);
}

function rerenderKind(kind) {
  if (!lastData) return;
  if (kind === "download") renderDownloads(lastData.download_candidates || [], lastData.download_summary || {});
  if (kind === "duplicate" || kind === "library") renderCandidates(kind, getRows(kind));
  if (kind === "quarantine") renderQuarantine(lastData.quarantined || { rows: [], items: 0, recoverable_size: "0 B" });
}

function syncSelection(input) {
  const kind = input.dataset.kind;
  const value = String(input.value);
  if (input.checked) selections[kind].add(value);
  else selections[kind].delete(value);
  const row = input.closest(".media-row");
  if (row) row.classList.toggle("selected", input.checked);
  renderSelectionBar(kind);
}

function selectVisible(kind) {
  const rows = visibleRows(kind, filteredRows(kind));
  if (!rows.length) return showMessage("There are no visible files to select.", false);
  const key = kind === "quarantine" ? "id" : "path";
  const allSelected = rows.every((row) => selections[kind].has(String(row[key])));
  rows.forEach((row) => {
    if (allSelected) selections[kind].delete(String(row[key]));
    else selections[kind].add(String(row[key]));
  });
  rerenderKind(kind);
}

function selectFiltered(kind) {
  const rows = filteredRows(kind);
  if (!rows.length) return showMessage("There are no files in this view to select.", false);
  const key = kind === "quarantine" ? "id" : "path";
  const allSelected = rows.every((row) => selections[kind].has(String(row[key])));
  rows.forEach((row) => {
    if (allSelected) selections[kind].delete(String(row[key]));
    else selections[kind].add(String(row[key]));
  });
  rerenderKind(kind);
}

function selectRecommended(kind) {
  const rows = filteredRows(kind);
  const key = kind === "quarantine" ? "id" : "path";
  const recommended = rows.filter((row) => {
    if (kind === "download") return row.confidence === "High";
    if (kind === "duplicate") return Boolean(row.keeper);
    return false;
  });
  if (!recommended.length) return showMessage("No recommended files are visible in this view.", false);
  recommended.forEach((row) => selections[kind].add(String(row[key])));
  rerenderKind(kind);
}

function clearSelection(kind) {
  selections[kind].clear();
  rerenderKind(kind);
}

function restoreSelection(kind) {
  document.querySelectorAll(`input[data-kind="${kind}"]`).forEach((input) => {
    input.checked = selections[kind].has(String(input.value));
    const row = input.closest(".media-row");
    if (row) row.classList.toggle("selected", input.checked);
  });
}

function renderSelectionBar(kind) {
  const target = document.getElementById(`${kind}Selection`);
  if (!target) return;
  const values = selections[kind];
  const rows = getRows(kind).filter((row) => values.has(String(kind === "quarantine" ? row.id : row.path)));
  const total = rows.reduce((sum, row) => sum + Number(row.size || 0), 0);
  target.classList.toggle("show", rows.length > 0);
  if (!rows.length) {
    target.innerHTML = "";
    return;
  }
  const actions = kind === "quarantine"
    ? `<button onclick="restoreSelected()">Restore</button><button class="danger" onclick="requestDelete()">Delete permanently</button>`
    : `<button class="primary" onclick="requestQuarantine('${kind}')">Review move</button>`;
  const visibleCount = visibleRows(kind, filteredRows(kind)).length;
  const matchingView = filteredRows(kind).filter((row) => values.has(String(kind === "quarantine" ? row.id : row.path))).length;
  target.innerHTML = `<div><strong>${rows.length} selected</strong><div class="sub">${humanSize(total)} - ${matchingView} in this view, ${visibleCount} shown</div></div><div class="selection-actions"><button onclick="clearSelection('${kind}')">Clear</button>${actions}</div>`;
}

function requestQuarantine(kind) {
  const rows = getRows(kind).filter((row) => selections[kind].has(String(row.path)));
  if (!rows.length) return showMessage("Select at least one file first.", false);
  const total = rows.reduce((sum, row) => sum + Number(row.size || 0), 0);
  const qbitOff = kind === "download" && lastData && !(lastData.protections || {}).qbittorrent_enabled;
  const qbitConfirmation = qbitOff
    ? `<div class="modal-warning"><strong>Seeding check is off.</strong> Confirm these downloads are finished before continuing.</div>
      <label class="confirm-check"><input type="checkbox" id="qbitConfirm"> I verified these downloads are not actively needed for seeding.</label>`
    : "";
  modalAction = { type: "quarantine", kind, paths: rows.map((row) => row.path) };
  document.getElementById("modalTitle").textContent = `Move ${rows.length} ${plural(rows.length, "file", "files")} to quarantine?`;
  document.getElementById("modalBody").innerHTML = `
    <div><strong>${humanSize(total)}</strong> will move out of its current folder and remain recoverable.</div>
    ${qbitConfirmation}
    <div class="modal-preview">${rows.slice(0, 6).map((row) => `<div>${escapeHtml(row.path)}</div>`).join("")}${rows.length > 6 ? `<div>+ ${rows.length - 6} more</div>` : ""}</div>`;
  const confirm = document.getElementById("modalConfirm");
  confirm.textContent = "Move to quarantine";
  confirm.className = "primary";
  confirm.disabled = qbitOff;
  openModal();
  const qbitConfirm = document.getElementById("qbitConfirm");
  if (qbitConfirm) qbitConfirm.addEventListener("change", () => { confirm.disabled = !qbitConfirm.checked; });
}

async function restoreSelected() {
  const ids = Array.from(selections.quarantine);
  if (!ids.length) return showMessage("Select at least one quarantined file first.", false);
  selections.quarantine.clear();
  await postAction("/restore", { ids }, "Restored to the original location.", {
    progressTitle: "Restoring",
    progressText: `Moving ${ids.length} ${plural(ids.length, "file", "files")} back...`,
    optimistic: { type: "quarantine", ids },
  });
}

function requestDelete() {
  const ids = Array.from(selections.quarantine);
  if (!ids.length) return showMessage("Select at least one quarantined file first.", false);
  const rows = getRows("quarantine").filter((row) => selections.quarantine.has(String(row.id)));
  const total = rows.reduce((sum, row) => sum + Number(row.size || 0), 0);
  modalAction = { type: "delete", ids };
  document.getElementById("modalTitle").textContent = `Permanently delete ${ids.length} ${plural(ids.length, "file", "files")}?`;
  document.getElementById("modalBody").innerHTML = `<div>This cannot be undone. Type <strong>DELETE</strong> to remove ${escapeHtml(humanSize(total))}.</div><input class="confirm-input" id="deleteConfirm" autocomplete="off" placeholder="Type DELETE" oninput="document.getElementById('modalConfirm').disabled=this.value !== 'DELETE'">`;
  const confirm = document.getElementById("modalConfirm");
  confirm.textContent = "Delete permanently";
  confirm.className = "danger";
  confirm.disabled = true;
  openModal();
  setTimeout(() => {
    const input = document.getElementById("deleteConfirm");
    if (input) input.focus();
  }, 50);
}

async function confirmModalAction() {
  if (!modalAction) return;
  const action = modalAction;
  closeModal();
  if (action.type === "quarantine") {
    selections[action.kind].clear();
    await postAction("/quarantine", { paths: action.paths }, "Moved to quarantine.", {
      progressTitle: "Quarantining",
      progressText: `Moving ${action.paths.length} ${plural(action.paths.length, "file", "files")} to quarantine...`,
      optimistic: { type: action.kind, paths: action.paths },
    });
  } else if (action.type === "delete") {
    selections.quarantine.clear();
    await postAction("/delete", { ids: action.ids, confirmation: "DELETE" }, "Permanently deleted.", {
      progressTitle: "Deleting",
      progressText: `Deleting ${action.ids.length} quarantined ${plural(action.ids.length, "file", "files")}...`,
      optimistic: { type: "quarantine", ids: action.ids },
    });
  }
}

function openModal() {
  document.getElementById("modalBackdrop").classList.add("show");
  document.body.style.overflow = "hidden";
}

function closeModal() {
  document.getElementById("modalBackdrop").classList.remove("show");
  document.body.style.overflow = "";
  modalAction = null;
}

function closeModalFromBackdrop(event) {
  if (event.target.id === "modalBackdrop") closeModal();
}

async function postAction(url, payload, successText, options = {}) {
  setActionProgress(true, options.progressTitle || "Working", options.progressText || "Starting...", 0);
  try {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
    if (!data.started) throw new Error(data.error || "File action could not start.");
    const status = await waitForAction(data.action && data.action.id, options);
    if (status.error) throw new Error(status.error);
    const result = status.result || {};
    applyOptimisticUpdate(updateFromActionResult(options.optimistic, result));
    if (result.errors && result.errors.length) {
      showMessage(formatActionErrors(result.errors), true);
    } else {
      showMessage(successText || "Done.", false);
    }
    await poll();
  } catch (error) {
    showMessage(error.message || String(error), true);
  } finally {
    setActionProgress(false, "Done", "Refreshing...", 100);
  }
}

function formatActionErrors(errors) {
  const messages = errors.map((error) => error.error || JSON.stringify(error));
  const shown = messages.slice(0, 3).join(" · ");
  return messages.length > 3 ? `${shown} · ${messages.length - 3} additional files were skipped.` : shown;
}

async function waitForAction(actionId, options = {}) {
  while (true) {
    const response = await fetch("/action-status");
    if (!response.ok) throw new Error(`Action status failed (${response.status})`);
    const status = await response.json();
    if (!actionId || status.id === actionId) {
      const total = Number(status.total || 0);
      const current = Number(status.current || 0);
      const percent = Number(status.percent || (total ? Math.round((current / total) * 100) : 0));
      const rowDetail = total ? `${current} of ${total}` : "Preparing files";
      const bytesCurrent = Number(status.bytes_current || 0);
      const bytesTotal = Number(status.bytes_total || 0);
      const transferDetail = bytesTotal ? ` · ${humanSize(bytesCurrent)} of ${humanSize(bytesTotal)}` : "";
      const detail = `${rowDetail}${transferDetail}`;
      setActionProgress(true, options.progressTitle || titleCase(status.kind || "Working"), `${status.label || options.progressText || "Working"} · ${detail}`, percent);
      if (!status.running) return status;
    }
    await delay(500);
  }
}

function updateFromActionResult(fallback, result) {
  if (result.moved && result.moved.length) {
    return { type: fallback?.type, paths: result.moved.map((row) => row.original_path).filter(Boolean) };
  }
  if (result.restored && result.restored.length) {
    return { type: "quarantine", ids: result.restored.map((row) => row.id).filter(Boolean) };
  }
  if (result.deleted && result.deleted.length) {
    return { type: "quarantine", ids: result.deleted.map((row) => row.id).filter(Boolean) };
  }
  return null;
}

function goTo(view) {
  activeView = document.getElementById(`view-${view}`) ? view : "overview";
  document.querySelectorAll(".view").forEach((section) => section.classList.toggle("active", section.id === `view-${activeView}`));
  document.querySelectorAll("[data-nav]").forEach((button) => button.classList.toggle("active", button.dataset.nav === activeView));
  if (location.hash !== `#${activeView}`) history.replaceState(null, "", `#${activeView}`);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function renderNavCounts(data) {
  document.getElementById("navDownloads").textContent = (data.download_summary || {}).items || 0;
  document.getElementById("navDuplicates").textContent = (data.duplicate_candidates || []).length;
  document.getElementById("navLibrary").textContent = (data.library_review || data.safe_candidates || []).length;
  document.getElementById("navQuarantine").textContent = (data.quarantined || {}).items || 0;
}

function applyOptimisticUpdate(update) {
  if (!update || !lastData) return;
  if (update.paths && update.paths.length) {
    const removed = new Set(update.paths.map(String));
    if (update.type === "download") {
      const before = lastData.download_candidates || [];
      const removedRows = before.filter((row) => removed.has(String(row.path)));
      lastData.download_candidates = before.filter((row) => !removed.has(String(row.path)));
      adjustDownloadSummary(removedRows);
    } else if (update.type === "duplicate") {
      lastData.duplicate_candidates = (lastData.duplicate_candidates || []).filter((row) => !removed.has(String(row.path)));
    } else if (update.type === "library") {
      lastData.library_review = (lastData.library_review || lastData.safe_candidates || []).filter((row) => !removed.has(String(row.path)));
      lastData.safe_candidates = lastData.library_review;
    }
    selections[update.type]?.clear();
    rerenderKind(update.type);
  }
  if (update.ids && update.ids.length && update.type === "quarantine") {
    const removedIds = new Set(update.ids.map(String));
    const quarantined = lastData.quarantined || { rows: [], items: 0 };
    quarantined.rows = (quarantined.rows || []).filter((row) => !removedIds.has(String(row.id)));
    quarantined.items = quarantined.rows.length;
    selections.quarantine.clear();
    lastData.quarantined = quarantined;
    renderQuarantine(quarantined);
  }
  renderNavCounts(lastData);
}

function adjustDownloadSummary(removedRows) {
  const summary = lastData.download_summary || {};
  const removedCount = removedRows.length;
  if (!removedCount) return;
  summary.items = Math.max(0, Number(summary.items || 0) - removedCount);
  summary.high_confidence = Math.max(0, Number(summary.high_confidence || 0) - removedRows.filter((row) => row.confidence === "High").length);
  summary.older_than_14_days = Math.max(0, Number(summary.older_than_14_days || 0) - removedRows.filter((row) => Number(row.age_days) >= 14).length);
  const remainingTotal = (lastData.download_candidates || []).reduce((sum, row) => sum + Number(row.size || 0), 0);
  const remainingLikely = (lastData.download_candidates || []).filter((row) => row.confidence === "High").reduce((sum, row) => sum + Number(row.size || 0), 0);
  summary.total_size = humanSize(remainingTotal);
  summary.likely_reclaimable = humanSize(remainingLikely);
  lastData.download_summary = summary;
}

function setActionProgress(show, title = "Working", text = "Updating files...", percent = 0) {
  const target = document.getElementById("actionProgress");
  if (!target) return;
  target.classList.toggle("show", Boolean(show));
  document.getElementById("actionProgressTitle").textContent = title;
  document.getElementById("actionProgressText").textContent = text;
  const bar = target.querySelector(".progress-track span");
  if (bar) bar.style.width = `${Math.max(0, Math.min(100, Number(percent || 0)))}%`;
}

function setStatus(isRunning, statusText, timeText) {
  document.getElementById("sideDot").classList.toggle("busy", isRunning);
  document.getElementById("sideStatus").textContent = statusText;
  document.getElementById("topStatus").textContent = statusText;
  if (timeText) {
    document.getElementById("sideTime").textContent = timeText;
    document.getElementById("topTime").textContent = timeText;
  }
}

function showMessage(text, error) {
  const toast = document.getElementById("toast");
  toast.textContent = text;
  toast.classList.toggle("error", Boolean(error));
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), error ? 9000 : 3500);
}

function emptyHtml(text) {
  return `<div class="empty">${escapeHtml(text)}</div>`;
}

function recommendationText(kind, row) {
  if (kind === "download") {
    if (row.keeper) return "Exact title and episode/year match found in the library.";
    if (row.confidence === "Medium") return "Older recognizable download; review seeding and library status before moving.";
    return row.reason || "Download file was not confirmed as imported by the media apps.";
  }
  if (kind === "duplicate") {
    if (row.keeper) return "Larger duplicate candidate paired with the library file that stays.";
    return row.reason || row.match || "Duplicate candidate needs review.";
  }
  if (kind === "library") {
    return row.reason || row.match || "Video file is in a library folder but was not confirmed by the media apps.";
  }
  return row.reason || "";
}

function humanSize(bytes) {
  let value = Number(bytes || 0);
  const units = ["B", "KB", "MB", "GB", "TB"];
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit ? 1 : 0)} ${units[unit]}`;
}

function formatDate(value) {
  if (!value) return "Unknown";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? String(value)
    : date.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function fileName(path) {
  return String(path || "").replace(/\\/g, "/").split("/").filter(Boolean).pop() || "Unknown file";
}

function parentPath(path) {
  const parts = String(path || "").replace(/\\/g, "/").split("/");
  parts.pop();
  return parts.join("/") || "/";
}

function plural(count, singular, pluralWord) {
  return Number(count) === 1 ? singular : pluralWord;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[character]);
}

function escapeAttr(value) {
  return escapeHtml(value).replace(/`/g, "&#96;");
}

function titleCase(value) {
  return String(value || "").replace(/_/g, " ").replace(/\b\w/g, (character) => character.toUpperCase());
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeModal();
});
goTo(location.hash.replace("#", "") || "overview");
poll();
setInterval(() => { if (!document.hidden) poll(); }, 30000);
if (running) setTimeout(poll, 1500);
