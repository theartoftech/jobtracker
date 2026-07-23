(function () {
  "use strict";

  if (!window.SkillGapAnalytics) throw new Error("SkillGapAnalytics must load before SkillGapDashboard");

  const analytics = window.SkillGapAnalytics;
  const appBasePath = window.location.pathname.startsWith("/jobtracker") ? "/jobtracker" : "";
  const summaryUrl = `${appBasePath}/api/analytics/skill-gaps`;

  /** @typedef {{skill_key:string, display_name:string, unique_affected_requisitions:number, required_count:number, preferred_count:number, active_count:number, resolved_count:number, high_fit_affected_roles:number, average_fit_score:number, first_observed:string, last_observed:string, trend:{previous_unique_requisitions:number, absolute_change:number, percent_change:number|null}, training:{status:string, target_date:string|null, notes:string|null, learning_resources:string[]}, training_priority:{score:number, hard_blocker_requisitions:number, eligible_affected_requisitions:number, components:Record<string,number>}}} SkillMetric */
  /** @typedef {{period:{days:number,start:string,end:string,previous_start:string,previous_end:string}, totals:Record<string,unknown>, skill_count:number, skills:SkillMetric[]}} Summary */
  /** @typedef {SkillMetric & {affected_postings:Array<Record<string,unknown>>, application_outcomes:{statuses:Record<string,number>, interview_outcomes:Record<string,number>}}} SkillDetail */

  const state = {
    loaded: false,
    loading: false,
    requestSequence: 0,
    detailRequestSequence: 0,
    summary: /** @type {Summary|null} */ (null),
    selectedSkillKey: "",
    sortKey: "priority",
    sortDirection: "desc"
  };

  const elements = {
    filters: document.querySelector("[data-analytics-filters]"),
    feedback: document.querySelector("[data-analytics-feedback]"),
    content: document.querySelector("[data-analytics-content]"),
    ranked: document.querySelector("[data-ranked-gaps]"),
    trendCanvas: document.querySelector("[data-trend-canvas]"),
    trendSummary: document.querySelector("[data-trend-summary]"),
    trendPeriod: document.querySelector("[data-trend-period]"),
    tableBody: document.querySelector("[data-skills-table]"),
    skillCount: document.querySelector("[data-skill-count]"),
    detail: document.querySelector("[data-skill-detail]"),
    detailTitle: document.querySelector("[data-skill-detail-title]"),
    detailSummary: document.querySelector("[data-skill-detail-summary]"),
    affectedPostings: document.querySelector("[data-affected-postings]"),
    closeDetail: document.querySelector("[data-close-skill-detail]")
  };

  initialize();

  /** @returns {void} */
  function initialize() {
    const missing = Object.entries(elements).filter((entry) => !entry[1]).map((entry) => entry[0]);
    if (missing.length) throw new Error(`Skill gap dashboard markup is incomplete: ${missing.join(", ")}`);
    elements.filters.addEventListener("change", loadSummary);
    document.querySelectorAll("[data-skill-sort]").forEach((button) => {
      button.addEventListener("click", () => changeSort(button.getAttribute("data-skill-sort") || ""));
    });
    elements.ranked.addEventListener("click", handleSkillSelection);
    elements.tableBody.addEventListener("click", handleSkillSelection);
    elements.closeDetail.addEventListener("click", closeDetail);
    elements.affectedPostings.addEventListener("click", openLinkedApplication);
    window.addEventListener("resize", redrawTrend);
  }

  /** @returns {Promise<void>} */
  async function activate() {
    if (!state.loaded && !state.loading) await loadSummary();
    else redrawTrend();
  }

  /** @returns {Record<string,string>} */
  function readFilters() {
    const formData = new FormData(elements.filters);
    return {
      days: String(formData.get("days") || "30"),
      classification: String(formData.get("classification") || ""),
      decision: String(formData.get("decision") || ""),
      state: String(formData.get("state") || "active")
    };
  }

  /** @returns {Promise<void>} */
  async function loadSummary() {
    const requestId = ++state.requestSequence;
    state.loading = true;
    state.selectedSkillKey = "";
    closeDetail();
    showFeedback("loading", "Loading skill gaps", "Reading the latest jobagent review data.");
    try {
      const query = analytics.buildSkillGapQuery(readFilters());
      const summary = await requestJson(`${summaryUrl}?${query}`);
      if (requestId !== state.requestSequence) return;
      validateSummary(summary);
      state.summary = summary;
      state.loaded = true;
      renderSummary(summary);
    } catch (error) {
      if (requestId !== state.requestSequence) return;
      state.summary = null;
      state.loaded = false;
      showFeedback("error", "Skill gaps could not load", readableError(error));
    } finally {
      if (requestId === state.requestSequence) state.loading = false;
    }
  }

  /**
   * @param {unknown} summary
   * @returns {asserts summary is Summary}
   */
  function validateSummary(summary) {
    if (!summary || typeof summary !== "object") throw new TypeError("Analytics response must be an object");
    const value = /** @type {Record<string,unknown>} */ (summary);
    if (!Array.isArray(value.skills)) throw new TypeError("Analytics response is missing skills");
    if (!value.period || typeof value.period !== "object") throw new TypeError("Analytics response is missing its period");
    if (!value.totals || typeof value.totals !== "object") throw new TypeError("Analytics response is missing KPI totals");
  }

  /**
   * @param {Summary} summary
   * @returns {void}
   */
  function renderSummary(summary) {
    elements.feedback.hidden = true;
    elements.content.hidden = false;
    const kpis = analytics.computeKpis(summary);
    setText("[data-analytics-kpi='active-gaps']", formatNumber(kpis.activeGaps));
    setText("[data-analytics-kpi='affected-jobs']", formatNumber(kpis.affectedJobs));
    setText("[data-analytics-kpi='high-fit']", formatNumber(kpis.highFitOpportunities));
    setText("[data-analytics-kpi='top-priority-score']", formatScore(kpis.topTrainingPriority.score));
    setText("[data-analytics-kpi='top-priority-label']", kpis.topTrainingPriority.label);
    elements.skillCount.textContent = `${summary.skill_count} ${summary.skill_count === 1 ? "skill" : "skills"}`;
    elements.trendPeriod.textContent = `${summary.period.days} days vs prior ${summary.period.days}`;
    renderRanked(summary.skills);
    renderTable(summary.skills);
    renderTrend(summary.skills);
    if (!summary.skills.length) {
      showFeedback("empty", "No skill gaps match", "Change a filter or wait for the next jobagent workflow import.");
    }
  }

  /**
   * @param {SkillMetric[]} skills
   * @returns {void}
   */
  function renderRanked(skills) {
    const ranked = analytics.rankSkills(skills, 8);
    const maximum = Math.max(1, ...ranked.map((skill) => Number(skill.unique_affected_requisitions) || 0));
    elements.ranked.innerHTML = ranked.length ? ranked.map((skill, index) => `
      <button class="ranked-gap-row" type="button" data-skill-key="${escapeAttribute(skill.skill_key)}">
        <span class="rank-number">${index + 1}</span>
        <span class="ranked-gap-body">
          <span class="ranked-gap-label"><strong>${escapeHtml(analytics.displaySkillName(skill))}</strong><span>${formatNumber(skill.unique_affected_requisitions)} ${pluralize(skill.unique_affected_requisitions, "job")}</span></span>
          <progress value="${finiteNumber(skill.unique_affected_requisitions)}" max="${maximum}">${formatNumber(skill.unique_affected_requisitions)} of ${maximum}</progress>
        </span>
      </button>
    `).join("") : `<div class="empty-state">No ranked gaps for this filter set.</div>`;
  }

  /**
   * @param {SkillMetric[]} skills
   * @returns {void}
   */
  function renderTable(skills) {
    const sorted = analytics.sortSkills(skills, state.sortKey, state.sortDirection);
    updateSortButtons();
    elements.tableBody.innerHTML = sorted.map((skill) => {
      const direction = analytics.trendDirection(skill.trend);
      const blockerCount = finiteNumber(skill.training_priority && skill.training_priority.hard_blocker_requisitions);
      return `
        <tr${state.selectedSkillKey === skill.skill_key ? " class=\"is-selected\"" : ""}>
          <th scope="row"><button class="skill-name-button" type="button" data-skill-key="${escapeAttribute(skill.skill_key)}">${escapeHtml(analytics.displaySkillName(skill))}</button><span>${formatNumber(skill.required_count)} required · ${formatNumber(skill.preferred_count)} preferred</span></th>
          <td><strong>${formatNumber(skill.unique_affected_requisitions)}</strong><span>unique jobs</span></td>
          <td><strong>${formatScore(skill.training_priority && skill.training_priority.score)}</strong><span>of 100</span></td>
          <td><strong>${formatNumber(blockerCount)}</strong><span>hard ${pluralize(blockerCount, "blocker")}</span></td>
          <td><span class="training-badge ${escapeAttribute(skill.training && skill.training.status || "not_evaluated")}">${escapeHtml(humanize(skill.training && skill.training.status || "not_evaluated"))}</span></td>
          <td><span class="trend-badge ${direction}">${escapeHtml(humanize(direction))}</span><span>${formatSigned(skill.trend && skill.trend.absolute_change)} jobs</span></td>
        </tr>`;
    }).join("");
  }

  /**
   * @param {SkillMetric[]} skills
   * @returns {void}
   */
  function renderTrend(skills) {
    const ranked = analytics.rankSkills(skills, 6);
    elements.trendSummary.innerHTML = ranked.map((skill) => {
      const direction = analytics.trendDirection(skill.trend);
      return `<span><strong>${escapeHtml(analytics.displaySkillName(skill))}</strong><em class="${direction}">${escapeHtml(humanize(direction))} ${formatSigned(skill.trend && skill.trend.absolute_change)}</em></span>`;
    }).join("");
    drawTrendChart(ranked);
  }

  /** @returns {void} */
  function redrawTrend() {
    if (state.summary && !elements.content.hidden) renderTrend(state.summary.skills);
  }

  /**
   * @param {SkillMetric[]} skills
   * @returns {void}
   */
  function drawTrendChart(skills) {
    const canvas = elements.trendCanvas;
    const context = canvas.getContext("2d");
    if (!context) throw new Error("Canvas rendering is unavailable");
    const cssWidth = Math.max(320, Math.floor(canvas.getBoundingClientRect().width || 760));
    const cssHeight = 300;
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.floor(cssWidth * ratio);
    canvas.height = Math.floor(cssHeight * ratio);
    context.scale(ratio, ratio);
    context.clearRect(0, 0, cssWidth, cssHeight);
    context.font = '700 12px "Avenir Next", Avenir, sans-serif';
    context.textBaseline = "middle";
    if (!skills.length) {
      context.fillStyle = "#627086";
      context.fillText("No trend data for this filter set.", 20, 42);
      return;
    }
    const labelWidth = Math.min(105, Math.floor(cssWidth * 0.25));
    const rightPadding = 42;
    const chartWidth = cssWidth - labelWidth - rightPadding;
    const rowHeight = Math.min(42, Math.floor((cssHeight - 24) / skills.length));
    const maximum = Math.max(1, ...skills.flatMap((skill) => [
      finiteNumber(skill.unique_affected_requisitions),
      finiteNumber(skill.trend && skill.trend.previous_unique_requisitions)
    ]));
    skills.forEach((skill, index) => {
      const centerY = 22 + index * rowHeight + rowHeight / 2;
      const previous = finiteNumber(skill.trend && skill.trend.previous_unique_requisitions);
      const current = finiteNumber(skill.unique_affected_requisitions);
      context.fillStyle = "#182033";
      context.fillText(analytics.displaySkillName(skill).slice(0, 14), 0, centerY);
      context.fillStyle = "#d9e0e8";
      context.fillRect(labelWidth, centerY - 10, chartWidth, 7);
      context.fillStyle = "#aab6c5";
      context.fillRect(labelWidth, centerY - 10, chartWidth * previous / maximum, 7);
      context.fillStyle = "#dbe7ff";
      context.fillRect(labelWidth, centerY + 3, chartWidth, 7);
      context.fillStyle = "#2563eb";
      context.fillRect(labelWidth, centerY + 3, chartWidth * current / maximum, 7);
      context.fillStyle = "#627086";
      context.fillText(`${previous} / ${current}`, cssWidth - rightPadding + 6, centerY);
    });
  }

  /**
   * @param {string} key
   * @returns {void}
   */
  function changeSort(key) {
    if (key === state.sortKey) state.sortDirection = state.sortDirection === "asc" ? "desc" : "asc";
    else {
      state.sortKey = key;
      state.sortDirection = key === "skill" || key === "training" ? "asc" : "desc";
    }
    if (state.summary) renderTable(state.summary.skills);
  }

  /** @returns {void} */
  function updateSortButtons() {
    document.querySelectorAll("[data-skill-sort]").forEach((button) => {
      const selected = button.getAttribute("data-skill-sort") === state.sortKey;
      const label = button.querySelector("span");
      if (label) label.textContent = selected ? (state.sortDirection === "asc" ? "Asc" : "Desc") : "";
      button.closest("th").setAttribute("aria-sort", selected ? (state.sortDirection === "asc" ? "ascending" : "descending") : "none");
    });
  }

  /**
   * @param {Event} event
   * @returns {void}
   */
  function handleSkillSelection(event) {
    const button = event.target.closest("[data-skill-key]");
    if (!button) return;
    loadDetail(button.getAttribute("data-skill-key") || "");
  }

  /**
   * @param {string} rawSkillKey
   * @returns {Promise<void>}
   */
  async function loadDetail(rawSkillKey) {
    const skillKey = analytics.normalizeSkillKey(rawSkillKey);
    const requestId = ++state.detailRequestSequence;
    state.selectedSkillKey = skillKey;
    if (state.summary) renderTable(state.summary.skills);
    elements.detail.hidden = false;
    elements.detailTitle.textContent = "Loading skill detail";
    elements.detailSummary.innerHTML = `<div class="detail-loading">Reading affected postings and application outcomes.</div>`;
    elements.affectedPostings.innerHTML = "";
    try {
      const query = analytics.buildSkillGapQuery(readFilters());
      const detail = /** @type {SkillDetail} */ (await requestJson(`${summaryUrl}/${encodeURIComponent(skillKey)}?${query}`));
      if (requestId !== state.detailRequestSequence) return;
      renderDetail(detail);
      elements.detail.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) {
      if (requestId !== state.detailRequestSequence) return;
      elements.detailTitle.textContent = "Skill detail unavailable";
      elements.detailSummary.innerHTML = `<div class="empty-state error-state">${escapeHtml(readableError(error))}</div>`;
    }
  }

  /**
   * @param {SkillDetail} detail
   * @returns {void}
   */
  function renderDetail(detail) {
    elements.detailTitle.textContent = `${analytics.displaySkillName(detail)} opportunities`;
    const statusSummary = formatCountMap(detail.application_outcomes && detail.application_outcomes.statuses);
    const interviewSummary = formatCountMap(detail.application_outcomes && detail.application_outcomes.interview_outcomes);
    elements.detailSummary.innerHTML = `
      <article><span>${formatNumber(detail.unique_affected_requisitions)}</span><p>Affected jobs</p></article>
      <article><span>${formatScore(detail.training_priority && detail.training_priority.score)}</span><p>Priority score</p></article>
      <article><span>${escapeHtml(statusSummary || "No linked outcomes")}</span><p>Application outcomes</p></article>
      <article><span>${escapeHtml(interviewSummary || "No interview outcomes")}</span><p>Interview outcomes</p></article>`;
    elements.affectedPostings.innerHTML = detail.affected_postings.length ? detail.affected_postings.map(renderPosting).join("") : `<div class="empty-state">No affected postings match these filters.</div>`;
  }

  /**
   * @param {Record<string,unknown>} posting
   * @returns {string}
   */
  function renderPosting(posting) {
    const outcome = posting.application_outcome && typeof posting.application_outcome === "object" ? posting.application_outcome : null;
    const interviews = outcome && Array.isArray(outcome.interview_outcomes) ? outcome.interview_outcomes.join(", ") : "";
    const outcomeText = outcome ? `${String(outcome.status || "Unspecified")}${interviews ? ` · ${interviews}` : ""}` : "No linked application";
    const applicationButton = posting.tracker_application_id ? `<button class="small-button" type="button" data-linked-application-id="${escapeAttribute(posting.tracker_application_id)}">Open application</button>` : "";
    return `
      <article class="affected-posting-card">
        <div class="affected-posting-heading">
          <div><h3>${escapeHtml(posting.company || "Company")}</h3><p>${escapeHtml(posting.title || "Untitled role")}</p></div>
          <span class="fit-score">${formatScore(posting.fit_score)} fit</span>
        </div>
        <dl>
          <div><dt>Decision</dt><dd>${escapeHtml(humanize(posting.workflow_decision || "unspecified"))}</dd></div>
          <div><dt>Qualification</dt><dd>${escapeHtml((Array.isArray(posting.classifications) ? posting.classifications : []).map(humanize).join(", "))}</dd></div>
          <div><dt>Gap state</dt><dd>${posting.active ? "Active" : "Resolved"}</dd></div>
          <div><dt>Application outcome</dt><dd>${escapeHtml(outcomeText)}</dd></div>
        </dl>
        <div class="affected-posting-actions">
          <a class="small-button" href="${escapeAttribute(posting.canonical_url)}" target="_blank" rel="noopener noreferrer">View posting</a>
          ${applicationButton}
          <span>Req ${escapeHtml(posting.requisition_id || "not supplied")}</span>
        </div>
      </article>`;
  }

  /**
   * @param {Event} event
   * @returns {void}
   */
  function openLinkedApplication(event) {
    const button = event.target.closest("[data-linked-application-id]");
    if (!button) return;
    const applicationId = button.getAttribute("data-linked-application-id") || "";
    window.dispatchEvent(new CustomEvent("jobtracker:open-application", { detail: { applicationId } }));
  }

  /** @returns {void} */
  function closeDetail() {
    state.detailRequestSequence += 1;
    state.selectedSkillKey = "";
    elements.detail.hidden = true;
    if (state.summary) renderTable(state.summary.skills);
  }

  /**
   * @param {"loading"|"error"|"empty"} type
   * @param {string} title
   * @param {string} message
   * @returns {void}
   */
  function showFeedback(type, title, message) {
    elements.content.hidden = type !== "empty";
    elements.feedback.hidden = false;
    elements.feedback.className = `analytics-feedback is-${type}`;
    elements.feedback.innerHTML = `${type === "loading" ? '<span class="analytics-loader" aria-hidden="true"></span>' : ""}<div><strong>${escapeHtml(title)}</strong><p>${escapeHtml(message)}</p>${type === "error" ? '<button class="small-button" type="button" data-retry-analytics>Retry</button>' : ""}</div>`;
    const retry = elements.feedback.querySelector("[data-retry-analytics]");
    if (retry) retry.addEventListener("click", loadSummary, { once: true });
  }

  /**
   * @param {string} url
   * @returns {Promise<Record<string,unknown>>}
   */
  async function requestJson(url) {
    const response = await fetch(url, { cache: "no-store", headers: { "Accept": "application/json" } });
    if (!response.ok) {
      const text = await response.text();
      throw new Error(`Analytics API returned ${response.status}${text ? `: ${summarize(text)}` : ""}`);
    }
    const value = await response.json();
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new TypeError("Analytics API returned invalid JSON");
    return value;
  }

  /**
   * @param {Record<string,number>|null|undefined} counts
   * @returns {string}
   */
  function formatCountMap(counts) {
    if (!counts || typeof counts !== "object") return "";
    return Object.entries(counts).sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0])).map(([label, count]) => `${label} ${count}`).join(", ");
  }

  /** @param {string} selector @param {string} value @returns {void} */
  function setText(selector, value) {
    const element = document.querySelector(selector);
    if (!element) throw new Error(`Missing dashboard element: ${selector}`);
    element.textContent = value;
  }

  /** @param {unknown} value @returns {number} */
  function finiteNumber(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : 0;
  }

  /** @param {unknown} value @returns {string} */
  function formatNumber(value) {
    return finiteNumber(value).toLocaleString("en-US", { maximumFractionDigits: 0 });
  }

  /** @param {unknown} value @returns {string} */
  function formatScore(value) {
    return finiteNumber(value).toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 2 });
  }

  /** @param {unknown} value @returns {string} */
  function formatSigned(value) {
    const number = finiteNumber(value);
    return `${number > 0 ? "+" : ""}${formatNumber(number)}`;
  }

  /** @param {unknown} value @param {string} singular @returns {string} */
  function pluralize(value, singular) {
    return finiteNumber(value) === 1 ? singular : `${singular}s`;
  }

  /** @param {unknown} value @returns {string} */
  function humanize(value) {
    return String(value || "").replace(/_/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
  }

  /** @param {unknown} error @returns {string} */
  function readableError(error) {
    return error instanceof Error ? error.message : "An unknown analytics error occurred.";
  }

  /** @param {unknown} value @returns {string} */
  function summarize(value) {
    return String(value || "").replace(/\s+/g, " ").replace(/<[^>]*>/g, " ").trim().slice(0, 160);
  }

  /** @param {unknown} value @returns {string} */
  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  /** @param {unknown} value @returns {string} */
  function escapeAttribute(value) {
    return escapeHtml(value).replace(/`/g, "&#96;");
  }

  window.SkillGapDashboard = { activate, loadSummary };
})();
