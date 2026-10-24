"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

/**
 * @typedef {{title?: string, custom?: boolean, deleted?: boolean, notes?: string}} TaskEntry
 * @typedef {{id: string, title: string}} TaskDefinition
 * @typedef {{
 *   state: {taskDays: Record<string, Record<string, TaskEntry>>, calendarLoaded: boolean,
 *     calendarConfigured: boolean, calendarError: string, calendarEvents: unknown[]},
 *   getTaskDefinitionsForDate: (dateKey: string) => TaskDefinition[],
 *   getDateKeyInZone: (date: Date) => string,
 *   dateToMinutes: (date: Date) => number,
 *   refreshCalendarEvents: () => Promise<void>
 * }} Tracker
 */

/** @param {number} status @param {Record<string, unknown>} config @returns {Tracker} */
function loadTracker(status = 204, config = {}) {
  const source = fs.readFileSync(require.resolve("../src/jobtracker.js"), "utf8")
    .replace("  initialize();", "")
    .replace(/\}\)\(\);\s*$/, `globalThis.tracker = {
      state, getTaskDefinitionsForDate, getDateKeyInZone, refreshCalendarEvents, dateToMinutes
    }; })();`);
  const context = vm.createContext({
    document: { querySelector: () => null, querySelectorAll: () => [] },
    window: { location: { pathname: "/" }, JOBTRACKER_CONFIG: {title: "Job Tracker", timeZone: "", defaultTasks: [], calendarOpenUrl: "", ...config} },
    fetch: async () => {
      if (status === 0) throw new Error("Network unavailable");
      return {
        ok: status >= 200 && status < 300,
        status,
        text: async () => "BEGIN:VCALENDAR\nEND:VCALENDAR"
      };
    }
  });
  vm.runInContext(source, context);
  return context.tracker;
}

test("new installations start with an empty daily checklist", () => {
  assert.equal(loadTracker().getTaskDefinitionsForDate("2026-01-01").length, 0);
});

test("saved tasks survive removal of built-in defaults, while deleted tasks stay hidden", () => {
  const tracker = loadTracker();
  tracker.state.taskDays["2026-01-01"] = {
    legacy: { title: "Saved task", custom: false, notes: "Keep these notes" },
    custom: { title: "Custom task", custom: true },
    deleted: { title: "Deleted task", deleted: true }
  };
  assert.deepEqual(Array.from(tracker.getTaskDefinitionsForDate("2026-01-01"), task => task.title),
    ["Saved task", "Custom task"]);
  assert.equal(tracker.state.taskDays["2026-01-01"].legacy.notes, "Keep these notes");
});

test("calendar dates use the device timezone around midnight", () => {
  const tracker = loadTracker();
  for (const iso of ["2026-01-01T00:30:00Z", "2026-07-01T23:30:00Z"]) {
    const date = new Date(iso);
    const localKey = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
    assert.equal(tracker.getDateKeyInZone(date), localKey);
  }
});

test("an unconfigured calendar clears stale events without reporting a successful sync", async () => {
  const tracker = loadTracker();
  tracker.state.calendarEvents = [{ stale: true }];
  await tracker.refreshCalendarEvents();
  assert.equal(tracker.state.calendarConfigured, false);
  assert.equal(tracker.state.calendarLoaded, false);
  assert.equal(tracker.state.calendarError, "");
  assert.equal(tracker.state.calendarEvents.length, 0);
});

test("a configured calendar can sync and an unavailable feed reports an error", async () => {
  const tracker = loadTracker(200);
  await tracker.refreshCalendarEvents();
  assert.equal(tracker.state.calendarConfigured, true);
  assert.equal(tracker.state.calendarLoaded, true);
  const unavailable = loadTracker(502);
  await unavailable.refreshCalendarEvents();
  assert.equal(unavailable.state.calendarLoaded, false);
  assert.equal(unavailable.state.calendarError, "feed unavailable");
});

test("a network failure after a disabled calendar is reported as unavailable", async () => {
  const tracker = loadTracker(0);
  tracker.state.calendarConfigured = false;
  await tracker.refreshCalendarEvents();
  assert.equal(tracker.state.calendarConfigured, true);
  assert.equal(tracker.state.calendarLoaded, false);
  assert.equal(tracker.state.calendarError, "feed unavailable");
});


test("deployment task defaults can be overridden and deleted by saved entries", () => {
  const tracker = loadTracker(204, {defaultTasks: [
    {id: "study", title: "Study", start: "10:00", end: "11:00"},
    {id: "follow-up", title: "Follow up", start: "11:00", end: "11:30"}
  ]});
  assert.equal(tracker.getTaskDefinitionsForDate("2026-01-01").length, 2);
  tracker.state.taskDays["2026-01-01"] = {study: {title: "Updated task"}, "follow-up": {deleted: true}};
  assert.deepEqual(Array.from(tracker.getTaskDefinitionsForDate("2026-01-01"), task => task.title), ["Updated task"]);
});

test("deployment timezone applies consistently to calendar dates and clock times", () => {
  const tracker = loadTracker(204, {timeZone: "Asia/Tokyo"});
  const date = new Date("2026-01-01T16:30:00Z");
  assert.equal(tracker.getDateKeyInZone(date), "2026-01-02");
  assert.equal(tracker.dateToMinutes(date), 90);
});
