(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.SkillGapAnalytics = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const FILTER_KEYS = ["days", "classification", "decision", "state", "company", "title", "training_status"];
  const SORT_KEYS = new Set(["skill", "frequency", "priority", "blockers", "training"]);
  const SORT_DIRECTIONS = new Set(["asc", "desc"]);
  const SKILL_KEY_PATTERN = /^[a-z0-9]+(?:[._+-][a-z0-9]+)*$/;
  const SKILL_ACRONYMS = Object.freeze({ aws: "AWS", gcp: "GCP", api: "API", sql: "SQL" });

  /** @typedef {{absolute_change?: number, previous_unique_requisitions?: number, percent_change?: number|null}} Trend */
  /** @typedef {{status?: string}} Training */
  /** @typedef {{score?: number, hard_blocker_requisitions?: number}} TrainingPriority */
  /** @typedef {{skill_key: string, display_name?: string, unique_affected_requisitions?: number, active_count?: number, required_count?: number, preferred_count?: number, high_fit_affected_roles?: number, training?: Training, training_priority?: TrainingPriority, trend?: Trend}} SkillMetric */
  /** @typedef {{active_gaps?: number, affected_jobs?: number, high_fit_opportunities?: number, top_training_priority?: {skill_key?: string, display_name?: string, score?: number}|null}} SummaryTotals */
  /** @typedef {{totals?: SummaryTotals, skills?: SkillMetric[]}} SkillSummary */

  /**
   * @param {Record<string, string|number|null|undefined>} filters
   * @returns {string}
   */
  function buildSkillGapQuery(filters) {
    const params = new URLSearchParams();
    FILTER_KEYS.forEach((key) => {
      const value = filters[key];
      if (value !== undefined && value !== null && String(value).trim() !== "") {
        params.set(key, String(value).trim());
      }
    });
    return params.toString();
  }

  /**
   * @param {SkillSummary} summary
   * @returns {{activeGaps: number, affectedJobs: number, highFitOpportunities: number, topTrainingPriority: {label: string, score: number}}}
   */
  function computeKpis(summary) {
    const totals = summary && summary.totals ? summary.totals : {};
    const top = totals.top_training_priority || null;
    return {
      activeGaps: finiteNumber(totals.active_gaps),
      affectedJobs: finiteNumber(totals.affected_jobs),
      highFitOpportunities: finiteNumber(totals.high_fit_opportunities),
      topTrainingPriority: {
        label: top ? displaySkillName(top) : "None",
        score: finiteNumber(top && top.score)
      }
    };
  }

  /**
   * @param {SkillMetric[]} skills
   * @param {number} limit
   * @returns {SkillMetric[]}
   */
  function rankSkills(skills, limit) {
    if (!Array.isArray(skills)) throw new TypeError("skills must be an array");
    if (!Number.isInteger(limit) || limit < 0) throw new TypeError("limit must be a non-negative integer");
    return skills.slice().sort((left, right) => {
      const frequency = finiteNumber(right.unique_affected_requisitions) - finiteNumber(left.unique_affected_requisitions);
      if (frequency) return frequency;
      const priority = finiteNumber(right.training_priority && right.training_priority.score) - finiteNumber(left.training_priority && left.training_priority.score);
      if (priority) return priority;
      return String(left.skill_key).localeCompare(String(right.skill_key));
    }).slice(0, limit);
  }

  /**
   * @param {SkillMetric[]} skills
   * @param {string} key
   * @param {string} direction
   * @returns {SkillMetric[]}
   */
  function sortSkills(skills, key, direction) {
    if (!Array.isArray(skills)) throw new TypeError("skills must be an array");
    if (!SORT_KEYS.has(key)) throw new TypeError(`Unsupported skill sort key: ${key}`);
    if (!SORT_DIRECTIONS.has(direction)) throw new TypeError(`Unsupported skill sort direction: ${direction}`);
    const multiplier = direction === "asc" ? 1 : -1;
    const sorted = skills.slice().sort((left, right) => {
      let comparison = 0;
      if (key === "skill") comparison = String(left.display_name || left.skill_key).localeCompare(String(right.display_name || right.skill_key));
      if (key === "frequency") comparison = finiteNumber(left.unique_affected_requisitions) - finiteNumber(right.unique_affected_requisitions);
      if (key === "priority") comparison = finiteNumber(left.training_priority && left.training_priority.score) - finiteNumber(right.training_priority && right.training_priority.score);
      if (key === "blockers") comparison = finiteNumber(left.training_priority && left.training_priority.hard_blocker_requisitions) - finiteNumber(right.training_priority && right.training_priority.hard_blocker_requisitions);
      if (key === "training") comparison = String(left.training && left.training.status || "not_evaluated").localeCompare(String(right.training && right.training.status || "not_evaluated"));
      return comparison ? comparison * multiplier : String(left.skill_key).localeCompare(String(right.skill_key));
    });
    return sorted;
  }

  /**
   * @param {Trend|null|undefined} trend
   * @returns {"increasing"|"decreasing"|"stable"}
   */
  function trendDirection(trend) {
    const change = finiteNumber(trend && trend.absolute_change);
    if (change > 0) return "increasing";
    if (change < 0) return "decreasing";
    return "stable";
  }

  /**
   * @param {unknown} value
   * @returns {string}
   */
  function normalizeSkillKey(value) {
    const skillKey = String(value || "").trim();
    if (!SKILL_KEY_PATTERN.test(skillKey)) throw new TypeError("skill_key must be a lowercase canonical skill key");
    return skillKey;
  }

  /**
   * @param {{skill_key?: unknown, display_name?: unknown}} skill
   * @returns {string}
   */
  function displaySkillName(skill) {
    if (!skill || typeof skill !== "object") throw new TypeError("skill must be an object");
    const skillKey = String(skill.skill_key || "").trim().toLowerCase();
    return SKILL_ACRONYMS[skillKey] || String(skill.display_name || skill.skill_key || "Unknown skill").trim();
  }

  /**
   * @param {unknown} value
   * @returns {number}
   */
  function finiteNumber(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : 0;
  }

  return {
    buildSkillGapQuery,
    computeKpis,
    displaySkillName,
    normalizeSkillKey,
    rankSkills,
    sortSkills,
    trendDirection
  };
});
