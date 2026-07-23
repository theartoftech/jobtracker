"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const analytics = require("../src/skill-gaps.js");

const skills = [
  {
    skill_key: "go",
    display_name: "Go",
    unique_affected_requisitions: 3,
    active_count: 3,
    required_count: 2,
    preferred_count: 1,
    high_fit_affected_roles: 2,
    training: { status: "planned" },
    training_priority: { score: 44.5 },
    trend: { previous_unique_requisitions: 1, absolute_change: 2, percent_change: 200 }
  },
  {
    skill_key: "aws",
    display_name: "AWS",
    unique_affected_requisitions: 5,
    active_count: 4,
    required_count: 5,
    preferred_count: 0,
    high_fit_affected_roles: 4,
    training: { status: "learning" },
    training_priority: { score: 71.25 },
    trend: { previous_unique_requisitions: 6, absolute_change: -1, percent_change: -16.67 }
  }
];

test("buildSkillGapQuery emits only supported non-empty filters", () => {
  assert.equal(
    analytics.buildSkillGapQuery({
      days: "60",
      classification: "required",
      decision: "apply",
      state: "resolved",
      ignored: "unsafe"
    }),
    "days=60&classification=required&decision=apply&state=resolved"
  );
  assert.equal(analytics.buildSkillGapQuery({ days: "30", classification: "" }), "days=30");
});

test("computeKpis uses server totals without double-counting affected jobs", () => {
  const summary = {
    totals: {
      active_gaps: 7,
      affected_jobs: 5,
      high_fit_opportunities: 4,
      top_training_priority: { skill_key: "aws", display_name: "AWS", score: 71.25 }
    },
    skills
  };
  assert.deepEqual(analytics.computeKpis(summary), {
    activeGaps: 7,
    affectedJobs: 5,
    highFitOpportunities: 4,
    topTrainingPriority: { label: "AWS", score: 71.25 }
  });
});

test("computeKpis and ranking handle an empty analytics result", () => {
  assert.deepEqual(analytics.computeKpis({ totals: {}, skills: [] }), {
    activeGaps: 0,
    affectedJobs: 0,
    highFitOpportunities: 0,
    topTrainingPriority: { label: "None", score: 0 }
  });
  assert.deepEqual(analytics.rankSkills([], 8), []);
});

test("rankSkills orders by frequency then priority without mutating input", () => {
  const original = skills.slice();
  assert.deepEqual(analytics.rankSkills(skills, 8).map((skill) => skill.skill_key), ["aws", "go"]);
  assert.deepEqual(skills, original);
});

test("sortSkills supports every dashboard column with deterministic ties", () => {
  assert.deepEqual(analytics.sortSkills(skills, "frequency", "desc").map((skill) => skill.skill_key), ["aws", "go"]);
  assert.deepEqual(analytics.sortSkills(skills, "priority", "asc").map((skill) => skill.skill_key), ["go", "aws"]);
  assert.deepEqual(analytics.sortSkills(skills, "blockers", "desc").map((skill) => skill.skill_key), ["aws", "go"]);
  assert.deepEqual(analytics.sortSkills(skills, "training", "asc").map((skill) => skill.skill_key), ["aws", "go"]);
  assert.deepEqual(analytics.sortSkills(skills, "skill", "desc").map((skill) => skill.skill_key), ["go", "aws"]);
});

test("trendDirection labels increasing, decreasing, and stable gaps", () => {
  assert.equal(analytics.trendDirection({ absolute_change: 2 }), "increasing");
  assert.equal(analytics.trendDirection({ absolute_change: -1 }), "decreasing");
  assert.equal(analytics.trendDirection({ absolute_change: 0 }), "stable");
  assert.equal(analytics.trendDirection(null), "stable");
});

test("normalizeSkillKey accepts canonical keys and rejects unsafe detail paths", () => {
  assert.equal(analytics.normalizeSkillKey("python-backend"), "python-backend");
  assert.throws(() => analytics.normalizeSkillKey("Python/../secrets"), /canonical skill key/);
});

test("displaySkillName preserves supplied labels and standardizes common acronyms", () => {
  assert.equal(analytics.displaySkillName({ skill_key: "aws", display_name: "Aws" }), "AWS");
  assert.equal(analytics.displaySkillName({ skill_key: "gcp", display_name: "Gcp" }), "GCP");
  assert.equal(analytics.displaySkillName({ skill_key: "go", display_name: "Go" }), "Go");
  assert.equal(analytics.displaySkillName({ skill_key: "custom", display_name: "Custom Skill" }), "Custom Skill");
});
