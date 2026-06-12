(function () {
  const STORAGE_KEY = "jh.jobtracker.v1";
  const ATTACHMENT_DB_NAME = "jh.jobtracker.attachments";
  const ATTACHMENT_DB_VERSION = 1;
  const ATTACHMENT_STORE = "files";
  const TASK_STORAGE_KEY = "jh.jobtracker.dailyTasks.v1";
  const APP_BASE_PATH = getAppBasePath();
  const API_HEALTH_URL = `${APP_BASE_PATH}/api/health`;
  const API_STATE_URL = `${APP_BASE_PATH}/api/state`;
  const API_ATTACHMENT_BASE = `${APP_BASE_PATH}/api/jobs`;
  const CALENDAR_FEED_URL = `${APP_BASE_PATH}/calendar.ics`;
  const MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024;
  const ATTACHMENT_CHUNK_BYTES = 512 * 1024;
  const ATTACHMENT_ACCEPT = {
    "application/pdf": [".pdf"],
    "application/msword": [".doc"],
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": [".docx"],
    "application/rtf": [".rtf"],
    "text/rtf": [".rtf"],
    "text/plain": [".txt"]
  };
  const TIME_ZONE = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const DEFAULT_TASKS = [];
  const STATUS_OPTIONS = [
    "Interested",
    "Applied",
    "Recruiter Screen",
    "Interviewing",
    "Final Round",
    "Offer",
    "Accepted",
    "Rejected",
    "Withdrawn",
    "Archived"
  ];
  const ACTIVE_STATUSES = new Set(["Interested", "Applied", "Recruiter Screen", "Interviewing", "Final Round", "Offer"]);

  const state = {
    jobs: [],
    selectedJobId: "",
    search: "",
    statusFilter: "",
    sort: "updated-desc",
    currentView: "applications",
    selectedTaskDate: formatDateInput(new Date()),
    taskDays: {},
    apiAvailable: false,
    saveTimer: 0,
    saveInFlight: Promise.resolve(),
    calendarEvents: [],
    calendarLoaded: false,
    calendarError: ""
  };

  const elements = {
    viewButtons: document.querySelectorAll("[data-view-button]"),
    viewPanels: document.querySelectorAll("[data-view-panel]"),
    form: document.querySelector("[data-application-form]"),
    interviewForm: document.querySelector("[data-interview-form]"),
    list: document.querySelector("[data-application-list]"),
    interviewList: document.querySelector("[data-interview-list]"),
    search: document.querySelector("[data-search]"),
    statusFilter: document.querySelector("[data-status-filter]"),
    sort: document.querySelector("[data-sort]"),
    companyList: document.querySelector("[data-company-list]"),
    recordCount: document.querySelector("[data-record-count]"),
    interviewCount: document.querySelector("[data-interview-count]"),
    detailTitle: document.querySelector("[data-detail-title]"),
    importJson: document.querySelector("[data-import-json]"),
    attachmentStates: document.querySelectorAll("[data-attachment-state]"),
    taskDate: document.querySelector("[data-task-date]"),
    taskList: document.querySelector("[data-task-list]"),
    taskCount: document.querySelector("[data-task-count]"),
    taskDayTitle: document.querySelector("[data-task-day-title]"),
    calendarStatus: document.querySelector("[data-calendar-status]"),
    calendarBusyList: document.querySelector("[data-calendar-busy-list]"),
    apiStatus: document.querySelector("[data-api-status]"),
    toast: document.querySelector("[data-toast]")
  };

  initialize();

  async function initialize() {
    populateStatusOptions();
    await loadState();
    wireEvents();
    wireTaskEvents();
    if (!state.selectedJobId && state.jobs.length) {
      state.selectedJobId = state.jobs[0].id;
    }
    render();
    updateApiStatus();
    refreshCalendarEvents();
  }

  function populateStatusOptions() {
    const statusSelect = elements.form.querySelector("[name='status']");
    statusSelect.innerHTML = STATUS_OPTIONS.map((status) => `<option>${escapeHtml(status)}</option>`).join("");
    elements.statusFilter.innerHTML = `<option value="">All statuses</option>${STATUS_OPTIONS.map((status) => `<option>${escapeHtml(status)}</option>`).join("")}`;
  }

  function wireEvents() {
    document.addEventListener("click", (event) => {
      const actionElement = event.target.closest("[data-action]");
      if (!actionElement) return;
      const action = actionElement.getAttribute("data-action");
      if (action === "new-job") createNewJob();
      if (action === "delete-job") deleteSelectedJob();
      if (action === "mark-status") markSelectedJobStatus(actionElement.getAttribute("data-status"));
      if (action === "export-json") exportJson();
      if (action === "export-csv") exportCsv();
      if (action === "clear-interview") clearInterviewForm();
      if (action === "choose-attachment") chooseAttachmentFile(actionElement.getAttribute("data-attachment-type"));
      if (action === "download-attachment") downloadAttachment(actionElement.getAttribute("data-attachment-type"));
      if (action === "remove-attachment") removeAttachment(actionElement.getAttribute("data-attachment-type"));
      if (action === "add-task") addTask();
      if (action === "delete-task") deleteTask(actionElement.getAttribute("data-task-id"));
      if (action === "previous-task-day") moveTaskDate(-1);
      if (action === "today-task-day") setTaskDate(formatDateInput(new Date()));
      if (action === "next-task-day") moveTaskDate(1);
    });

    elements.list.addEventListener("click", (event) => {
      const card = event.target.closest("[data-job-id]");
      if (!card) return;
      cacheApplicationDraft();
      state.selectedJobId = card.getAttribute("data-job-id");
      clearInterviewForm();
      persistState(false);
      render();
    });

    elements.interviewList.addEventListener("click", (event) => {
      const editButton = event.target.closest("[data-edit-interview]");
      const deleteButton = event.target.closest("[data-delete-interview]");
      if (editButton) editInterview(editButton.getAttribute("data-edit-interview"));
      if (deleteButton) deleteInterview(deleteButton.getAttribute("data-delete-interview"));
    });

    elements.form.addEventListener("submit", (event) => {
      event.preventDefault();
      saveApplication();
    });

    elements.form.addEventListener("input", cacheApplicationDraft);
    elements.form.addEventListener("change", cacheApplicationDraft);

    elements.interviewForm.addEventListener("submit", (event) => {
      event.preventDefault();
      saveInterview();
    });

    elements.search.addEventListener("input", () => {
      state.search = elements.search.value.trim();
      renderApplicationList();
    });

    elements.statusFilter.addEventListener("change", () => {
      state.statusFilter = elements.statusFilter.value;
      renderApplicationList();
    });

    elements.sort.addEventListener("change", () => {
      state.sort = elements.sort.value;
      renderApplicationList();
    });

    elements.importJson.addEventListener("change", importJson);

    document.querySelectorAll("[data-file-input]").forEach((input) => {
      input.addEventListener("change", () => handleAttachmentUpload(input));
    });
  }

  function wireTaskEvents() {
    elements.viewButtons.forEach((button) => {
      button.addEventListener("click", () => setCurrentView(button.getAttribute("data-view-button")));
    });

    if (elements.taskDate) {
      elements.taskDate.addEventListener("change", () => setTaskDate(elements.taskDate.value));
    }

    if (elements.taskList) {
      elements.taskList.addEventListener("change", (event) => {
        const checkbox = event.target.closest("[data-task-done]");
        if (checkbox) {
          updateTaskEntry(checkbox.getAttribute("data-task-done"), { done: checkbox.checked });
          return;
        }
        const field = event.target.closest("[data-task-field]");
        if (!field) return;
        updateTaskDefinition(
          field.getAttribute("data-task-id"),
          { [field.getAttribute("data-task-field")]: field.value },
          true
        );
      });

      elements.taskList.addEventListener("input", (event) => {
        const notes = event.target.closest("[data-task-notes]");
        if (notes) {
          updateTaskEntry(notes.getAttribute("data-task-notes"), { notes: notes.value }, false);
          return;
        }
        const field = event.target.closest("[data-task-field]");
        if (!field || field.type === "time") return;
        updateTaskDefinition(
          field.getAttribute("data-task-id"),
          { [field.getAttribute("data-task-field")]: field.value },
          false
        );
      });
    }
  }

  function setCurrentView(view) {
    state.currentView = view === "tasks" ? "tasks" : "applications";
    elements.viewButtons.forEach((button) => {
      button.classList.toggle("is-active", button.getAttribute("data-view-button") === state.currentView);
    });
    elements.viewPanels.forEach((panel) => {
      panel.hidden = panel.getAttribute("data-view-panel") !== state.currentView;
    });
    if (state.currentView === "tasks") renderTasks();
  }

  function createNewJob() {
    const now = new Date().toISOString();
    const job = {
      id: createId(),
      company: "",
      title: "",
      status: "Applied",
      appliedDate: formatDateInput(new Date()),
      source: "",
      location: "",
      salary: "",
      jobUrl: "",
      resumeName: "",
      resumeLink: "",
      coverLetterName: "",
      coverLetterLink: "",
      resumeFileName: "",
      resumeFileSize: 0,
      resumeFileType: "",
      resumeFileUpdatedAt: "",
      coverLetterFileName: "",
      coverLetterFileSize: 0,
      coverLetterFileType: "",
      coverLetterFileUpdatedAt: "",
      jobDescription: "",
      notes: "",
      interviews: [],
      createdAt: now,
      updatedAt: now
    };
    state.jobs.unshift(job);
    state.selectedJobId = job.id;
    persistState(false);
    render();
    elements.form.querySelector("[name='company']").focus();
  }

  function saveApplication() {
    const formData = new FormData(elements.form);
    const id = String(formData.get("id") || "");
    let job = getSelectedJob();
    if (!job || job.id !== id) {
      job = { id: id || createId(), interviews: [], createdAt: new Date().toISOString() };
      state.jobs.unshift(job);
      state.selectedJobId = job.id;
    }

    applyApplicationFormValues(job, formData);

    persistState();
    render();
  }

  function markSelectedJobStatus(status) {
    const normalizedStatus = STATUS_OPTIONS.includes(status) ? status : "";
    if (!normalizedStatus) return;
    const job = getSelectedJob();
    if (!job) {
      showToast("Select or create an application first.");
      return;
    }
    const formData = new FormData(elements.form);
    applyApplicationFormValues(job, formData);
    job.status = normalizedStatus;
    job.updatedAt = new Date().toISOString();
    elements.form.querySelector("[name='status']").value = normalizedStatus;
    persistState();
    render();
    showToast(`Marked ${normalizedStatus}.`);
  }

  function cacheApplicationDraft() {
    const job = getSelectedJob();
    if (!job) return;
    const formData = new FormData(elements.form);
    const id = String(formData.get("id") || "");
    if (id && id !== job.id) return;
    applyApplicationFormValues(job, formData);
    persistState(false);
  }

  function applyApplicationFormValues(job, formData) {
    Object.assign(job, {
      company: clean(formData.get("company")),
      title: clean(formData.get("title")),
      status: clean(formData.get("status")) || "Applied",
      appliedDate: clean(formData.get("appliedDate")),
      source: clean(formData.get("source")),
      location: clean(formData.get("location")),
      salary: clean(formData.get("salary")),
      jobUrl: clean(formData.get("jobUrl")),
      resumeName: clean(formData.get("resumeName")),
      resumeLink: clean(formData.get("resumeLink")),
      coverLetterName: clean(formData.get("coverLetterName")),
      coverLetterLink: clean(formData.get("coverLetterLink")),
      jobDescription: clean(formData.get("jobDescription")),
      notes: clean(formData.get("notes")),
      updatedAt: new Date().toISOString()
    });
  }

  async function deleteSelectedJob() {
    const job = getSelectedJob();
    if (!job) return;
    const label = [job.company, job.title].filter(Boolean).join(" - ") || "this application";
    if (!window.confirm(`Delete ${label}?`)) return;
    await deleteJobAttachments(job.id);
    state.jobs = state.jobs.filter((item) => item.id !== job.id);
    state.selectedJobId = state.jobs[0] ? state.jobs[0].id : "";
    persistState();
    render();
  }

  function saveInterview() {
    const job = getSelectedJob();
    if (!job) {
      showToast("Select or create an application first.");
      return;
    }
    const formData = new FormData(elements.interviewForm);
    const id = clean(formData.get("id")) || createId();
    const interview = {
      id,
      round: clean(formData.get("round")),
      date: clean(formData.get("date")),
      time: clean(formData.get("time")),
      format: clean(formData.get("format")),
      interviewer: clean(formData.get("interviewer")),
      outcome: clean(formData.get("outcome")),
      followUpDate: clean(formData.get("followUpDate")),
      location: clean(formData.get("location")),
      notes: clean(formData.get("notes")),
      updatedAt: new Date().toISOString()
    };
    job.interviews = Array.isArray(job.interviews) ? job.interviews : [];
    const existingIndex = job.interviews.findIndex((item) => item.id === id);
    if (existingIndex >= 0) {
      job.interviews[existingIndex] = interview;
    } else {
      job.interviews.push(interview);
    }
    job.updatedAt = new Date().toISOString();
    clearInterviewForm();
    persistState();
    render();
  }

  function editInterview(interviewId) {
    const job = getSelectedJob();
    const interview = job && Array.isArray(job.interviews)
      ? job.interviews.find((item) => item.id === interviewId)
      : null;
    if (!interview) return;
    setFormValues(elements.interviewForm, interview);
    elements.interviewForm.querySelector("[name='round']").focus();
  }

  function deleteInterview(interviewId) {
    const job = getSelectedJob();
    if (!job || !Array.isArray(job.interviews)) return;
    job.interviews = job.interviews.filter((item) => item.id !== interviewId);
    job.updatedAt = new Date().toISOString();
    persistState();
    render();
  }

  function clearInterviewForm() {
    elements.interviewForm.reset();
    elements.interviewForm.querySelector("[name='id']").value = "";
  }

  function render() {
    setCurrentView(state.currentView);
    elements.search.value = state.search;
    elements.statusFilter.value = state.statusFilter;
    elements.sort.value = state.sort;
    renderMetrics();
    renderCompanyList();
    renderApplicationList();
    renderDetail();
    renderTasks();
  }

  function renderMetrics() {
    const total = state.jobs.length;
    const active = state.jobs.filter((job) => ACTIVE_STATUSES.has(job.status)).length;
    const interviews = state.jobs.reduce((count, job) => count + (Array.isArray(job.interviews) ? job.interviews.length : 0), 0);
    const offers = state.jobs.filter((job) => job.status === "Offer" || job.status === "Accepted").length;
    setMetric("total", total);
    setMetric("active", active);
    setMetric("interviews", interviews);
    setMetric("offers", offers);
  }

  function renderCompanyList() {
    const companyCounts = new Map();
    state.jobs.forEach((job) => {
      const company = job.company || "Unassigned";
      companyCounts.set(company, (companyCounts.get(company) || 0) + 1);
    });
    const companies = Array.from(companyCounts.entries()).sort((a, b) => a[0].localeCompare(b[0]));
    elements.companyList.innerHTML = companies.length
      ? companies.map(([company, count]) => `
          <article class="company-pill">
            <strong>${escapeHtml(company)}</strong>
            <p>${count} ${count === 1 ? "application" : "applications"}</p>
          </article>
        `).join("")
      : `<div class="empty-state">No companies yet.</div>`;
  }

  function renderApplicationList() {
    const jobs = getFilteredJobs();
    elements.recordCount.textContent = `${jobs.length} ${jobs.length === 1 ? "record" : "records"}`;
    elements.list.innerHTML = jobs.length
      ? jobs.map((job) => renderApplicationCard(job)).join("")
      : `<div class="empty-state">No applications match the current filters.</div>`;
  }

  function renderApplicationCard(job) {
    const selectedClass = job.id === state.selectedJobId ? " is-selected" : "";
    const statusClass = getStatusClass(job.status);
    const applied = job.appliedDate ? `Applied ${formatDisplayDate(job.appliedDate)}` : "No applied date";
    const interviewCount = Array.isArray(job.interviews) ? job.interviews.length : 0;
    const jobLink = job.jobUrl ? `<a href="${escapeAttribute(job.jobUrl)}" target="_blank" rel="noopener">Job post</a>` : "";
    const resumeLink = job.resumeLink ? `<a href="${escapeAttribute(job.resumeLink)}" target="_blank" rel="noopener">Resume</a>` : "";
    const coverLetterLink = job.coverLetterLink ? `<a href="${escapeAttribute(job.coverLetterLink)}" target="_blank" rel="noopener">Cover letter</a>` : "";
    return `
      <article class="application-card${selectedClass}" data-job-id="${escapeAttribute(job.id)}" tabindex="0">
        <div class="application-title-row">
          <div>
            <h3>${escapeHtml(job.company || "Company")}</h3>
            <p class="application-meta">${escapeHtml(job.title || "Position")}</p>
          </div>
          <span class="status-badge ${statusClass}">${escapeHtml(job.status || "Applied")}</span>
        </div>
        <div class="application-status-row">
          <p class="application-meta">${escapeHtml(applied)} - ${interviewCount} ${interviewCount === 1 ? "interview" : "interviews"}</p>
          <p class="application-meta">${escapeHtml(job.location || "")}</p>
        </div>
        <div class="application-links">${jobLink}${resumeLink}${coverLetterLink}</div>
      </article>
    `;
  }

  function renderDetail() {
    const job = getSelectedJob();
    if (!job) {
      elements.detailTitle.textContent = "New Application";
      elements.form.reset();
      elements.form.querySelector("[name='id']").value = "";
      elements.interviewList.innerHTML = `<div class="empty-state">Create an application to add interviews.</div>`;
      elements.interviewCount.textContent = "0 records";
      renderAttachments(null);
      return;
    }
    elements.detailTitle.textContent = [job.company, job.title].filter(Boolean).join(" - ") || "New Application";
    setFormValues(elements.form, job);
    renderAttachments(job);
    renderInterviews(job);
  }

  function renderAttachments(job) {
    elements.attachmentStates.forEach((element) => {
      const type = element.getAttribute("data-attachment-state");
      const metadata = job ? getAttachmentMetadata(job, type) : null;
      element.textContent = metadata && metadata.name
        ? `${metadata.name} (${formatFileSize(metadata.size)})`
        : `No ${type === "resume" ? "resume" : "cover letter"} file stored.`;
      document.querySelectorAll(`[data-attachment-type='${type}']`).forEach((button) => {
        const action = button.getAttribute("data-action");
        if (action === "download-attachment" || action === "remove-attachment") {
          button.disabled = !(metadata && metadata.name);
        }
      });
    });
  }

  function renderInterviews(job) {
    const interviews = Array.isArray(job.interviews) ? [...job.interviews] : [];
    interviews.sort((a, b) => String(a.date || "").localeCompare(String(b.date || "")));
    elements.interviewCount.textContent = `${interviews.length} ${interviews.length === 1 ? "record" : "records"}`;
    elements.interviewList.innerHTML = interviews.length
      ? interviews.map((interview) => `
          <article class="interview-card">
            <div class="interview-card-header">
              <div>
                <h3>${escapeHtml(interview.round || "Interview")}</h3>
                <p class="interview-meta">${escapeHtml(formatInterviewWhen(interview))}</p>
              </div>
              <span class="status-badge ${getStatusClass(interview.outcome)}">${escapeHtml(interview.outcome || "Pending")}</span>
            </div>
            <p>${escapeHtml([interview.format, interview.interviewer, interview.location].filter(Boolean).join(" - "))}</p>
            <p>${escapeHtml(interview.notes || "")}</p>
            ${interview.followUpDate ? `<p class="interview-meta">Follow up ${escapeHtml(formatDisplayDate(interview.followUpDate))}</p>` : ""}
            <div class="interview-card-actions">
              <button class="small-button" type="button" data-edit-interview="${escapeAttribute(interview.id)}">Edit</button>
              <button class="small-button" type="button" data-delete-interview="${escapeAttribute(interview.id)}">Delete</button>
            </div>
          </article>
        `).join("")
      : `<div class="empty-state">No interviews recorded for this application.</div>`;
  }

  function renderTasks() {
    if (!elements.taskList) return;
    const dateKey = state.selectedTaskDate || formatDateInput(new Date());
    const entries = getTaskDay(dateKey);
    const taskDefinitions = getTaskDefinitionsForDate(dateKey);
    const schedule = getScheduledTasks(dateKey);
    const completed = taskDefinitions.filter((task) => entries[task.id] && entries[task.id].done).length;
    const totalHours = schedule.reduce((sum, item) => sum + Math.max(0, item.endMinutes - item.startMinutes), 0) / 60;

    elements.taskDate.value = dateKey;
    elements.taskDayTitle.textContent = formatLongDate(dateKey);
    elements.taskCount.textContent = `${taskDefinitions.length} ${taskDefinitions.length === 1 ? "task" : "tasks"}`;
    setTaskMetric("completed", `${completed} / ${taskDefinitions.length}`);
    setTaskMetric("hours", `${formatHours(totalHours)} hrs`);
    setTaskMetric("calendar", state.calendarLoaded ? "Synced" : state.calendarError ? "Offline" : "Loading");

    elements.taskList.innerHTML = schedule.map((item) => {
      const entry = entries[item.id] || {};
      const completeClass = entry.done ? " is-complete" : "";
      const shiftedClass = item.shifted ? " is-shifted" : "";
      return `
        <article class="task-card${completeClass}${shiftedClass}">
          <label class="task-check">
            <input type="checkbox" data-task-done="${escapeAttribute(item.id)}"${entry.done ? " checked" : ""}>
            <span></span>
          </label>
          <div class="task-body">
            <div class="task-title-row">
              <div class="task-edit-fields">
                <label>
                  <span>Task</span>
                  <input value="${escapeAttribute(item.title)}" data-task-id="${escapeAttribute(item.id)}" data-task-field="title">
                </label>
                <label>
                  <span>Detail</span>
                  <input value="${escapeAttribute(item.detail || "")}" data-task-id="${escapeAttribute(item.id)}" data-task-field="detail">
                </label>
              </div>
              <div class="task-time">
                <div class="task-time-edit">
                  <label>
                    <span>Start</span>
                    <input type="time" value="${escapeAttribute(item.start)}" data-task-id="${escapeAttribute(item.id)}" data-task-field="start">
                  </label>
                  <label>
                    <span>End</span>
                    <input type="time" value="${escapeAttribute(item.end)}" data-task-id="${escapeAttribute(item.id)}" data-task-field="end">
                  </label>
                </div>
                <strong>${escapeHtml(formatMinutes(item.startMinutes))} - ${escapeHtml(formatMinutes(item.endMinutes))}</strong>
                ${item.shifted ? `<span>Moved from ${escapeHtml(item.start)} - ${escapeHtml(item.end)}</span>` : ""}
                <button class="small-button danger" type="button" data-action="delete-task" data-task-id="${escapeAttribute(item.id)}">Delete</button>
              </div>
            </div>
            <label class="task-notes">
              <span>Notes</span>
              <textarea rows="3" data-task-notes="${escapeAttribute(item.id)}" placeholder="Notes for this task">${escapeHtml(entry.notes || "")}</textarea>
            </label>
          </div>
        </article>
      `;
    }).join("");

    renderCalendarBusyBlocks(dateKey);
  }

  function renderCalendarBusyBlocks(dateKey) {
    if (!elements.calendarStatus || !elements.calendarBusyList) return;
    const busyBlocks = getBusyBlocksForDate(dateKey);
    elements.calendarStatus.textContent = state.calendarError
      ? `Calendar could not sync: ${state.calendarError}`
      : state.calendarLoaded
        ? `Using ${busyBlocks.length} busy ${busyBlocks.length === 1 ? "block" : "blocks"} for ${formatLongDate(dateKey)}.`
        : "Loading calendar busy blocks.";
    elements.calendarBusyList.innerHTML = busyBlocks.length
      ? busyBlocks.map((block) => `
          <article class="busy-item">
            <strong>${escapeHtml(formatMinutes(block.startMinutes))} - ${escapeHtml(formatMinutes(block.endMinutes))}</strong>
            <span>${block.allDay ? "All day" : "Busy"}</span>
          </article>
        `).join("")
      : `<div class="empty-state">No calendar conflicts for this day.</div>`;
  }

  function getScheduledTasks(dateKey) {
    const busyBlocks = getBusyBlocksForDate(dateKey).filter((block) => !block.allDay);
    let previousEnd = 0;
    return getTaskDefinitionsForDate(dateKey).map((task) => {
      const duration = Math.max(15, timeToMinutes(task.end) - timeToMinutes(task.start));
      const originalStart = timeToMinutes(task.start);
      let startMinutes = Math.max(originalStart, previousEnd);
      let endMinutes = startMinutes + duration;
      let changed = true;

      while (changed) {
        changed = false;
        for (const block of busyBlocks) {
          if (rangesOverlap(startMinutes, endMinutes, block.startMinutes, block.endMinutes)) {
            startMinutes = Math.max(startMinutes, block.endMinutes);
            endMinutes = startMinutes + duration;
            changed = true;
          }
        }
      }

      previousEnd = endMinutes;
      return {
        ...task,
        startMinutes,
        endMinutes,
        shifted: startMinutes !== originalStart
      };
    });
  }

  function getTaskDefinitionsForDate(dateKey) {
    const day = state.taskDays[dateKey] && typeof state.taskDays[dateKey] === "object" ? state.taskDays[dateKey] : {};
    const defaultTaskIds = new Set(DEFAULT_TASKS.map((task) => task.id));
    const defaultTasks = DEFAULT_TASKS
      .map((task, index) => mergeTaskDefinition(task, day[task.id], index))
      .filter((task) => !task.deleted);
    const customTasks = Object.entries(day)
      .filter(([taskId, entry]) => !defaultTaskIds.has(taskId) && entry && typeof entry === "object" && entry.custom && !entry.deleted)
      .map(([taskId, entry], index) => mergeTaskDefinition({
        id: taskId,
        title: "New task",
        detail: "",
        start: "09:00",
        end: "09:30",
        custom: true
      }, entry, DEFAULT_TASKS.length + index));
    return [...defaultTasks, ...customTasks].sort((a, b) => {
      if (a.sortOrder !== b.sortOrder) return a.sortOrder - b.sortOrder;
      return timeToMinutes(a.start) - timeToMinutes(b.start);
    });
  }

  function mergeTaskDefinition(task, entry, fallbackOrder) {
    const data = entry && typeof entry === "object" ? entry : {};
    return {
      ...task,
      title: clean(data.title) || task.title || "Task",
      detail: clean(data.detail) || task.detail || "",
      start: normalizeTimeValue(data.start) || task.start || "09:00",
      end: normalizeTimeValue(data.end) || task.end || "09:30",
      custom: Boolean(data.custom || task.custom),
      deleted: Boolean(data.deleted),
      sortOrder: Number(data.sortOrder) || fallbackOrder
    };
  }

  function getBusyBlocksForDate(dateKey) {
    return state.calendarEvents
      .filter((event) => event.dateKey === dateKey)
      .map((event) => {
        if (event.allDay) {
          return { startMinutes: 0, endMinutes: 24 * 60, allDay: true };
        }
        return {
          startMinutes: dateToMinutes(event.start),
          endMinutes: Math.max(dateToMinutes(event.end), dateToMinutes(event.start)),
          allDay: false
        };
      })
      .filter((block) => block.endMinutes > block.startMinutes)
      .sort((a, b) => a.startMinutes - b.startMinutes);
  }

  function moveTaskDate(dayOffset) {
    const date = parseDateKey(state.selectedTaskDate);
    date.setDate(date.getDate() + dayOffset);
    setTaskDate(formatDateInput(date));
  }

  function setTaskDate(dateKey) {
    if (!dateKey) return;
    state.selectedTaskDate = dateKey;
    persistTaskState();
    renderTasks();
  }

  function addTask() {
    const dateKey = state.selectedTaskDate || formatDateInput(new Date());
    const day = getTaskDay(dateKey);
    const existingTasks = getTaskDefinitionsForDate(dateKey);
    const lastTask = existingTasks[existingTasks.length - 1];
    const startMinutes = lastTask ? Math.min((23 * 60) + 30, timeToMinutes(lastTask.end)) : 9 * 60;
    const endMinutes = Math.min((23 * 60) + 59, startMinutes + 30);
    const taskId = `custom-${createId()}`;
    day[taskId] = {
      custom: true,
      title: "New task",
      detail: "",
      start: formatMinutes(startMinutes),
      end: formatMinutes(endMinutes),
      notes: "",
      done: false,
      sortOrder: existingTasks.length
    };
    persistTaskState();
    renderTasks();
    showToast("Task added.");
  }

  function deleteTask(taskId) {
    if (!taskId) return;
    const dateKey = state.selectedTaskDate || formatDateInput(new Date());
    const day = getTaskDay(dateKey);
    const entry = day[taskId] || {};
    if (entry.custom) {
      delete day[taskId];
    } else {
      day[taskId] = { ...entry, deleted: true, done: false };
    }
    persistTaskState();
    renderTasks();
    showToast("Task deleted.");
  }

  function updateTaskEntry(taskId, updates, shouldRender) {
    if (!taskId) return;
    const dateKey = state.selectedTaskDate || formatDateInput(new Date());
    const day = getTaskDay(dateKey);
    day[taskId] = { ...(day[taskId] || {}), ...updates };
    persistTaskState();
    if (shouldRender !== false) renderTasks();
  }

  function updateTaskDefinition(taskId, updates, shouldRender) {
    if (!taskId) return;
    const dateKey = state.selectedTaskDate || formatDateInput(new Date());
    const day = getTaskDay(dateKey);
    const current = day[taskId] || {};
    const normalizedUpdates = { ...updates };
    if (Object.prototype.hasOwnProperty.call(normalizedUpdates, "start")) {
      normalizedUpdates.start = normalizeTimeValue(normalizedUpdates.start);
    }
    if (Object.prototype.hasOwnProperty.call(normalizedUpdates, "end")) {
      normalizedUpdates.end = normalizeTimeValue(normalizedUpdates.end);
    }
    day[taskId] = { ...current, ...normalizedUpdates };
    persistTaskState();
    if (shouldRender !== false) renderTasks();
  }

  function getTaskDay(dateKey) {
    if (!state.taskDays[dateKey]) state.taskDays[dateKey] = {};
    return state.taskDays[dateKey];
  }

  function loadTaskState() {
    try {
      const parsed = JSON.parse(window.localStorage.getItem(TASK_STORAGE_KEY) || "{}");
      state.selectedTaskDate = clean(parsed.selectedTaskDate) || formatDateInput(new Date());
      state.taskDays = parsed.taskDays && typeof parsed.taskDays === "object" ? parsed.taskDays : {};
    } catch (error) {
      state.selectedTaskDate = formatDateInput(new Date());
      state.taskDays = {};
    }
  }

  function persistTaskState() {
    window.localStorage.setItem(TASK_STORAGE_KEY, JSON.stringify({
      version: 1,
      selectedTaskDate: state.selectedTaskDate,
      taskDays: state.taskDays
    }));
    queueRemotePersist(false);
  }

  async function refreshCalendarEvents() {
    try {
      const response = await fetch(CALENDAR_FEED_URL, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const content = await response.text();
      state.calendarEvents = parseCalendarEvents(content);
      state.calendarLoaded = true;
      state.calendarError = "";
    } catch (error) {
      state.calendarEvents = [];
      state.calendarLoaded = false;
      state.calendarError = "feed unavailable";
    }
    renderTasks();
  }

  function parseCalendarEvents(content) {
    const unfolded = String(content || "").replace(/\r?\n[ \t]/g, "");
    const matches = unfolded.match(/BEGIN:VEVENT[\s\S]*?END:VEVENT/g) || [];
    return matches.map(parseCalendarEvent).filter(Boolean);
  }

  function parseCalendarEvent(block) {
    const startLine = findIcsLine(block, "DTSTART");
    const endLine = findIcsLine(block, "DTEND");
    if (!startLine) return null;
    const startValue = parseIcsDate(startLine);
    const endValue = parseIcsDate(endLine) || startValue;
    if (!startValue || !endValue) return null;
    return {
      start: startValue.date,
      end: endValue.date,
      allDay: startValue.allDay || endValue.allDay,
      dateKey: startValue.allDay ? startValue.dateKey : getDateKeyInZone(startValue.date)
    };
  }

  function findIcsLine(block, name) {
    return String(block || "").split(/\r?\n/).find((line) => line.startsWith(`${name}:`) || line.startsWith(`${name};`)) || "";
  }

  function parseIcsDate(line) {
    if (!line) return null;
    const separatorIndex = line.indexOf(":");
    if (separatorIndex < 0) return null;
    const meta = line.slice(0, separatorIndex);
    const value = line.slice(separatorIndex + 1).trim();
    if (!value) return null;

    if (meta.includes("VALUE=DATE")) {
      const year = Number(value.slice(0, 4));
      const month = Number(value.slice(4, 6)) - 1;
      const day = Number(value.slice(6, 8));
      const date = new Date(year, month, day);
      return { date, allDay: true, dateKey: formatDateInput(date) };
    }

    const year = Number(value.slice(0, 4));
    const month = Number(value.slice(4, 6)) - 1;
    const day = Number(value.slice(6, 8));
    const hour = Number(value.slice(9, 11)) || 0;
    const minute = Number(value.slice(11, 13)) || 0;
    const second = Number(value.slice(13, 15)) || 0;
    const date = value.endsWith("Z")
      ? new Date(Date.UTC(year, month, day, hour, minute, second))
      : new Date(year, month, day, hour, minute, second);
    if (Number.isNaN(date.getTime())) return null;
    return { date, allDay: false, dateKey: getDateKeyInZone(date) };
  }

  function getFilteredJobs() {
    const query = state.search.toLowerCase();
    return state.jobs
      .filter((job) => {
        const matchesStatus = !state.statusFilter || job.status === state.statusFilter;
        const haystack = [
          job.company,
          job.title,
          job.status,
          job.source,
          job.location,
          job.resumeName,
          job.coverLetterName,
          job.jobDescription,
          job.notes,
          ...(Array.isArray(job.interviews) ? job.interviews.flatMap((interview) => [interview.round, interview.interviewer, interview.notes]) : [])
        ].join(" ").toLowerCase();
        return matchesStatus && (!query || haystack.includes(query));
      })
      .sort(sortJobs);
  }

  function sortJobs(a, b) {
    if (state.sort === "company-asc") return String(a.company || "").localeCompare(String(b.company || ""));
    if (state.sort === "status-asc") return String(a.status || "").localeCompare(String(b.status || ""));
    if (state.sort === "applied-desc") return String(b.appliedDate || "").localeCompare(String(a.appliedDate || ""));
    return String(b.updatedAt || "").localeCompare(String(a.updatedAt || ""));
  }

  function exportJson() {
    downloadFile(`jobtracker-${formatDateInput(new Date())}.json`, JSON.stringify({
      version: 2,
      exportedAt: new Date().toISOString(),
      selectedJobId: state.selectedJobId,
      selectedTaskDate: state.selectedTaskDate,
      jobs: state.jobs,
      taskDays: state.taskDays
    }, null, 2), "application/json");
    showToast("JSON export ready.");
  }

  function exportCsv() {
    const header = ["Company", "Position", "Status", "Date Applied", "Source", "Location", "Salary", "Job URL", "Resume Sent", "Resume File", "Resume Link", "Cover Letter Sent", "Cover Letter File", "Cover Letter Link", "Interview Count", "Next Interview", "Notes"];
    const rows = state.jobs.map((job) => [job.company, job.title, job.status, job.appliedDate, job.source, job.location, job.salary, job.jobUrl, job.resumeName, job.resumeFileName, job.resumeLink, job.coverLetterName, job.coverLetterFileName, job.coverLetterLink, Array.isArray(job.interviews) ? job.interviews.length : 0, getNextInterviewDate(job), job.notes]);
    const csv = [header, ...rows].map((row) => row.map(csvCell).join(",")).join("\n");
    downloadFile(`jobtracker-${formatDateInput(new Date())}.csv`, csv, "text/csv");
    showToast("CSV export ready.");
  }

  async function handleAttachmentUpload(input) {
    const type = input.getAttribute("data-file-input");
    const file = input.files && input.files[0];
    await handleAttachmentFile(type, file, input);
  }

  async function chooseAttachmentFile(type) {
    const input = document.querySelector(`[data-file-input='${type}']`);
    if (window.showOpenFilePicker) {
      try {
        const handles = await window.showOpenFilePicker({
          multiple: false,
          types: [{ description: "Documents", accept: ATTACHMENT_ACCEPT }]
        });
        const file = handles && handles[0] ? await handles[0].getFile() : null;
        await handleAttachmentFile(type, file, input);
        return;
      } catch (error) {
        if (error && error.name === "AbortError") return;
        console.error("Attachment picker failed", error);
        showToast(`The file could not be selected. ${formatErrorMessage(error)}`);
        return;
      }
    }
    if (input) input.click();
  }

  async function handleAttachmentFile(type, file, input) {
    const job = getSelectedJob();
    if (!job) {
      showToast("Create or select an application first.");
      if (input) input.value = "";
      return;
    }
    if (!file || !type) return;
    if (file.size > MAX_ATTACHMENT_BYTES) {
      showToast("The file is larger than the 50 MB upload limit.");
      if (input) input.value = "";
      return;
    }

    try {
      const readableFile = await readSelectedFile(file);
      const metadata = await saveAttachment(job.id, type, readableFile);
      applyAttachmentMetadata(job, type, metadata || readableFile);
      if (type === "resume" && !job.resumeName) job.resumeName = readableFile.name;
      if (type === "coverLetter" && !job.coverLetterName) job.coverLetterName = readableFile.name;
      job.updatedAt = new Date().toISOString();
      persistState(false);
      render();
      showToast(`${type === "resume" ? "Resume" : "Cover letter"} file stored in database.`);
    } catch (error) {
      console.error("Attachment upload failed", error);
      showToast(`The file could not be stored. ${formatErrorMessage(error)}`);
    } finally {
      if (input) input.value = "";
    }
  }

  async function downloadAttachment(type) {
    const job = getSelectedJob();
    if (!job || !type) return;
    try {
      const record = await getAttachment(job.id, type);
      if (!record || !record.blob) {
        showToast("That stored file was not found.");
        return;
      }
      downloadBlob(record.blob, record.name || `${type}-attachment`);
    } catch (error) {
      showToast("The stored file could not be downloaded.");
    }
  }

  async function removeAttachment(type) {
    const job = getSelectedJob();
    if (!job || !type) return;
    const label = type === "resume" ? "resume file" : "cover letter file";
    if (!window.confirm(`Remove the stored ${label}?`)) return;
    await deleteAttachment(job.id, type);
    clearAttachmentMetadata(job, type);
    job.updatedAt = new Date().toISOString();
    persistState(false);
    render();
    showToast("Stored file removed.");
  }

  function importJson(event) {
    const file = event.target.files && event.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      try {
        const parsed = JSON.parse(String(reader.result || "{}"));
        const jobs = Array.isArray(parsed) ? parsed : parsed.jobs;
        if (!Array.isArray(jobs)) throw new Error("Missing jobs array");
        state.jobs = jobs.map(normalizeJob);
        state.selectedJobId = clean(parsed.selectedJobId) || (state.jobs[0] ? state.jobs[0].id : "");
        if (parsed.taskDays && typeof parsed.taskDays === "object") state.taskDays = parsed.taskDays;
        if (parsed.selectedTaskDate) state.selectedTaskDate = clean(parsed.selectedTaskDate);
        persistState();
        render();
        showToast("JSON import complete.");
      } catch (error) {
        showToast("That JSON file could not be imported.");
      } finally {
        event.target.value = "";
      }
    };
    reader.readAsText(file);
  }

  async function loadState() {
    const localState = readLocalState();
    const localTaskState = readLocalTaskState();
    let shouldMigrateLocalAttachments = false;
    try {
      const response = await fetch(API_STATE_URL, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const parsed = await response.json();
      setApiAvailable(true);
      state.jobs = Array.isArray(parsed.jobs) ? parsed.jobs.map(normalizeJob) : [];
      state.selectedJobId = clean(parsed.selectedJobId) || localState.selectedJobId || "";
      state.selectedTaskDate = clean(parsed.selectedTaskDate) || localTaskState.selectedTaskDate || formatDateInput(new Date());
      state.taskDays = parsed.taskDays && typeof parsed.taskDays === "object" ? parsed.taskDays : {};

      if (!state.jobs.length && localState.jobs.length) {
        state.jobs = localState.jobs;
        state.selectedJobId = localState.selectedJobId || state.jobs[0].id;
        shouldMigrateLocalAttachments = true;
      }
      if (!Object.keys(state.taskDays).length && Object.keys(localTaskState.taskDays).length) {
        state.taskDays = localTaskState.taskDays;
      }
      if (shouldMigrateLocalAttachments) {
        await migrateLocalAttachments();
      }
      writeLocalBackup();
      if (localState.jobs.length || Object.keys(localTaskState.taskDays).length) {
        queueRemotePersist(false, 0);
      }
    } catch (error) {
      setApiAvailable(false);
      state.jobs = localState.jobs;
      state.selectedJobId = localState.selectedJobId;
      state.selectedTaskDate = localTaskState.selectedTaskDate || formatDateInput(new Date());
      state.taskDays = localTaskState.taskDays;
      showToast("Database unavailable. Using browser backup.");
    }
  }

  function persistState(showSavedToast) {
    writeLocalBackup();
    queueRemotePersist(showSavedToast !== false);
    if (!state.apiAvailable && showSavedToast !== false) showToast("Saved to browser backup.");
  }

  function readLocalState() {
    try {
      const parsed = JSON.parse(window.localStorage.getItem(STORAGE_KEY) || "{}");
      return {
        jobs: Array.isArray(parsed.jobs) ? parsed.jobs.map(normalizeJob) : [],
        selectedJobId: clean(parsed.selectedJobId)
      };
    } catch (error) {
      return { jobs: [], selectedJobId: "" };
    }
  }

  function readLocalTaskState() {
    try {
      const parsed = JSON.parse(window.localStorage.getItem(TASK_STORAGE_KEY) || "{}");
      return {
        selectedTaskDate: clean(parsed.selectedTaskDate),
        taskDays: parsed.taskDays && typeof parsed.taskDays === "object" ? parsed.taskDays : {}
      };
    } catch (error) {
      return { selectedTaskDate: "", taskDays: {} };
    }
  }

  function writeLocalBackup() {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify({
      version: 2,
      selectedJobId: state.selectedJobId,
      jobs: state.jobs
    }));
    window.localStorage.setItem(TASK_STORAGE_KEY, JSON.stringify({
      version: 2,
      selectedTaskDate: state.selectedTaskDate,
      taskDays: state.taskDays
    }));
  }

  function queueRemotePersist(showSavedToast, delay) {
    if (!state.apiAvailable) return;
    window.clearTimeout(state.saveTimer);
    state.saveTimer = window.setTimeout(() => {
      state.saveInFlight = state.saveInFlight
        .catch(() => {})
        .then(() => saveStateToApi(showSavedToast));
    }, typeof delay === "number" ? delay : 350);
  }

  async function saveStateToApi(showSavedToast) {
    try {
      const response = await fetch(API_STATE_URL, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          version: 2,
          selectedJobId: state.selectedJobId,
          selectedTaskDate: state.selectedTaskDate,
          jobs: state.jobs,
          taskDays: state.taskDays
        })
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setApiAvailable(true);
      if (showSavedToast) showToast("Saved to database.");
    } catch (error) {
      setApiAvailable(false);
      if (showSavedToast) showToast("Database save failed. Browser backup kept.");
    }
  }

  function normalizeJob(job) {
    const now = new Date().toISOString();
    return {
      id: clean(job.id) || createId(),
      company: clean(job.company),
      title: clean(job.title),
      status: STATUS_OPTIONS.includes(job.status) ? job.status : clean(job.status) || "Applied",
      appliedDate: clean(job.appliedDate),
      source: clean(job.source),
      location: clean(job.location),
      salary: clean(job.salary),
      jobUrl: clean(job.jobUrl),
      resumeName: clean(job.resumeName || job.resume),
      resumeLink: clean(job.resumeLink),
      resumeFileName: clean(job.resumeFileName),
      resumeFileSize: Number(job.resumeFileSize) || 0,
      resumeFileType: clean(job.resumeFileType),
      resumeFileUpdatedAt: clean(job.resumeFileUpdatedAt),
      coverLetterName: clean(job.coverLetterName || job.coverLetter),
      coverLetterLink: clean(job.coverLetterLink),
      coverLetterFileName: clean(job.coverLetterFileName),
      coverLetterFileSize: Number(job.coverLetterFileSize) || 0,
      coverLetterFileType: clean(job.coverLetterFileType),
      coverLetterFileUpdatedAt: clean(job.coverLetterFileUpdatedAt),
      jobDescription: clean(job.jobDescription),
      notes: clean(job.notes),
      interviews: Array.isArray(job.interviews) ? job.interviews.map(normalizeInterview) : [],
      createdAt: clean(job.createdAt) || now,
      updatedAt: clean(job.updatedAt) || now
    };
  }

  function normalizeInterview(interview) {
    return {
      id: clean(interview.id) || createId(),
      round: clean(interview.round || interview.activityName),
      date: clean(interview.date || interview.startDate),
      time: clean(interview.time || interview.startTime),
      format: clean(interview.format || interview.activityType),
      interviewer: clean(interview.interviewer),
      outcome: clean(interview.outcome),
      followUpDate: clean(interview.followUpDate),
      location: clean(interview.location),
      notes: clean(interview.notes || interview.description),
      updatedAt: clean(interview.updatedAt) || new Date().toISOString()
    };
  }

  function getSelectedJob() {
    return state.jobs.find((job) => job.id === state.selectedJobId) || null;
  }

  function setFormValues(form, values) {
    Array.from(form.elements).forEach((field) => {
      if (!field.name) return;
      field.value = values[field.name] || "";
    });
  }

  function setMetric(name, value) {
    const element = document.querySelector(`[data-metric='${name}']`);
    if (element) element.textContent = String(value);
  }

  function showToast(message) {
    if (!elements.toast) return;
    elements.toast.textContent = message;
    elements.toast.classList.add("is-visible");
    window.clearTimeout(showToast.timeout);
    showToast.timeout = window.setTimeout(() => elements.toast.classList.remove("is-visible"), 1800);
  }

  function setApiAvailable(available) {
    state.apiAvailable = Boolean(available);
    updateApiStatus();
  }

  function updateApiStatus() {
    if (!elements.apiStatus) return;
    elements.apiStatus.textContent = state.apiAvailable ? "Database connected" : "Browser backup only";
    elements.apiStatus.classList.toggle("is-connected", state.apiAvailable);
    elements.apiStatus.classList.toggle("is-offline", !state.apiAvailable);
  }

  function downloadFile(filename, content, type) {
    const blob = new Blob([content], { type });
    downloadBlob(blob, filename);
  }

  function downloadBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }

  function openAttachmentDb() {
    return new Promise((resolve, reject) => {
      const request = window.indexedDB.open(ATTACHMENT_DB_NAME, ATTACHMENT_DB_VERSION);
      request.onupgradeneeded = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains(ATTACHMENT_STORE)) {
          db.createObjectStore(ATTACHMENT_STORE, { keyPath: "id" });
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  }

  async function saveAttachment(jobId, type, file) {
    if (!state.apiAvailable) {
      await refreshApiAvailability();
    }
    if (!state.apiAvailable) {
      throw new Error("Database unavailable. Reload the tracker and confirm the status badge says Database connected.");
    }
    try {
      return await uploadAttachmentToApi(jobId, type, file, file.name);
    } catch (error) {
      setApiAvailable(false);
      throw error;
    }
  }

  async function refreshApiAvailability() {
    try {
      const response = await fetch(API_HEALTH_URL, { cache: "no-store" });
      setApiAvailable(response.ok);
    } catch (error) {
      setApiAvailable(false);
    }
  }

  async function getAttachment(jobId, type) {
    if (state.apiAvailable) {
      try {
        const response = await fetch(getAttachmentUrl(jobId, type), { cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const disposition = response.headers.get("Content-Disposition") || "";
        const nameMatch = disposition.match(/filename="([^"]+)"/);
        return {
          blob: await response.blob(),
          name: nameMatch ? nameMatch[1] : `${type}-attachment`
        };
      } catch (error) {
        setApiAvailable(false);
      }
    }

    return getLocalAttachment(jobId, type);
  }

  async function deleteAttachment(jobId, type) {
    if (state.apiAvailable) {
      try {
        const response = await fetch(getAttachmentUrl(jobId, type), { method: "DELETE" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return;
      } catch (error) {
        setApiAvailable(false);
      }
    }

    const db = await openAttachmentDb();
    return runAttachmentTransaction(db, "readwrite", (store) => {
      store.delete(getAttachmentKey(jobId, type));
    });
  }

  async function deleteJobAttachments(jobId) {
    await Promise.all([
      deleteAttachment(jobId, "resume"),
      deleteAttachment(jobId, "coverLetter")
    ]);
  }

  function runAttachmentTransaction(db, mode, callback) {
    return new Promise((resolve, reject) => {
      const transaction = db.transaction(ATTACHMENT_STORE, mode);
      callback(transaction.objectStore(ATTACHMENT_STORE));
      transaction.oncomplete = () => {
        db.close();
        resolve();
      };
      transaction.onerror = () => {
        db.close();
        reject(transaction.error);
      };
      transaction.onabort = () => {
        db.close();
        reject(transaction.error);
      };
    });
  }

  function getAttachmentKey(jobId, type) {
    return `${jobId}:${type}`;
  }

  async function uploadAttachmentToApi(jobId, type, file, filename) {
    const uploadId = createId();
    const totalChunks = Math.max(1, Math.ceil(file.size / ATTACHMENT_CHUNK_BYTES));
    let metadata = null;
    for (let index = 0; index < totalChunks; index += 1) {
      const start = index * ATTACHMENT_CHUNK_BYTES;
      const end = Math.min(file.size, start + ATTACHMENT_CHUNK_BYTES);
      const chunkBytes = file.bytes.subarray(start, end);
      const response = await fetch(getAttachmentChunkUrl(jobId, type, uploadId, index), {
        method: "PUT",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          fileName: filename || file.name || `${type}-attachment`,
          fileType: file.type || "application/octet-stream",
          totalChunks,
          totalSize: file.size,
          finalChunk: index === totalChunks - 1,
          contentBase64: bytesToBase64(chunkBytes)
        })
      });
      if (!response.ok) {
        const detail = await response.text().catch(() => "");
        throw new Error(`Upload failed: HTTP ${response.status}${response.statusText ? ` ${response.statusText}` : ""}${detail ? ` - ${summarizeResponseText(detail)}` : ""}`);
      }
      const parsed = await response.json();
      if (parsed && parsed.metadata) metadata = parsed.metadata;
    }
    return metadata;
  }

  async function readSelectedFile(file) {
    try {
      const buffer = await file.arrayBuffer();
      return {
        name: file.name,
        type: file.type || "application/octet-stream",
        size: file.size,
        bytes: new Uint8Array(buffer)
      };
    } catch (error) {
      throw new Error("The selected file could not be read. Move it to a local folder such as Downloads, make sure it is not open in another app or still downloading from cloud storage, then choose it again.");
    }
  }

  function bytesToBase64(bytes) {
    let binary = "";
    const batchSize = 0x8000;
    for (let index = 0; index < bytes.length; index += batchSize) {
      binary += String.fromCharCode(...bytes.subarray(index, index + batchSize));
    }
    return btoa(binary);
  }

  async function getLocalAttachment(jobId, type) {
    const db = await openAttachmentDb();
    return new Promise((resolve, reject) => {
      const transaction = db.transaction(ATTACHMENT_STORE, "readonly");
      const request = transaction.objectStore(ATTACHMENT_STORE).get(getAttachmentKey(jobId, type));
      request.onsuccess = () => resolve(request.result || null);
      request.onerror = () => reject(request.error);
      transaction.oncomplete = () => db.close();
      transaction.onerror = () => {
        db.close();
        reject(transaction.error);
      };
    });
  }

  async function migrateLocalAttachments() {
    if (!state.apiAvailable || !window.indexedDB) return;
    for (const job of state.jobs) {
      for (const type of ["resume", "coverLetter"]) {
        const metadata = getAttachmentMetadata(job, type);
        if (!metadata.name) continue;
        try {
          const record = await getLocalAttachment(job.id, type);
          if (!record || !record.blob) continue;
          const uploaded = await uploadAttachmentToApi(job.id, type, record.blob, record.name || metadata.name);
          if (uploaded) applyAttachmentMetadata(job, type, uploaded);
        } catch (error) {
          return;
        }
      }
    }
  }

  function getAttachmentUrl(jobId, type) {
    return `${API_ATTACHMENT_BASE}/${encodeURIComponent(jobId)}/attachments/${encodeURIComponent(type)}`;
  }

  function getAttachmentChunkUrl(jobId, type, uploadId, index) {
    return `${getAttachmentUrl(jobId, type)}/chunks/${encodeURIComponent(uploadId)}/${index}`;
  }

  function getAppBasePath() {
    const path = window.location.pathname || "/";
    const match = path.match(/^(.*\/jobtracker)(?:\/|$)/);
    if (!match) return "";
    return match[1].replace(/\/$/, "");
  }

  function applyAttachmentMetadata(job, type, fileOrMetadata) {
    const prefix = getAttachmentPrefix(type);
    job[`${prefix}FileName`] = fileOrMetadata[`${prefix}FileName`] || fileOrMetadata.name || "";
    job[`${prefix}FileSize`] = Number(fileOrMetadata[`${prefix}FileSize`] || fileOrMetadata.size) || 0;
    job[`${prefix}FileType`] = fileOrMetadata[`${prefix}FileType`] || fileOrMetadata.type || "application/octet-stream";
    job[`${prefix}FileUpdatedAt`] = fileOrMetadata[`${prefix}FileUpdatedAt`] || new Date().toISOString();
  }

  function clearAttachmentMetadata(job, type) {
    const prefix = getAttachmentPrefix(type);
    job[`${prefix}FileName`] = "";
    job[`${prefix}FileSize`] = 0;
    job[`${prefix}FileType`] = "";
    job[`${prefix}FileUpdatedAt`] = "";
  }

  function getAttachmentMetadata(job, type) {
    const prefix = getAttachmentPrefix(type);
    return {
      name: job[`${prefix}FileName`] || "",
      size: Number(job[`${prefix}FileSize`]) || 0,
      type: job[`${prefix}FileType`] || "",
      updatedAt: job[`${prefix}FileUpdatedAt`] || ""
    };
  }

  function getAttachmentPrefix(type) {
    return type === "coverLetter" ? "coverLetter" : "resume";
  }

  function formatFileSize(bytes) {
    const size = Number(bytes) || 0;
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
    return `${(size / (1024 * 1024)).toFixed(2)} MB`;
  }

  function formatErrorMessage(error) {
    return summarizeResponseText(error && error.message ? error.message : String(error || ""));
  }

  function summarizeResponseText(value) {
    return String(value || "")
      .replace(/\s+/g, " ")
      .replace(/<[^>]*>/g, " ")
      .trim()
      .slice(0, 180);
  }

  function getNextInterviewDate(job) {
    const today = formatDateInput(new Date());
    const interviews = Array.isArray(job.interviews) ? job.interviews : [];
    return interviews.map((interview) => interview.date).filter((date) => date && date >= today).sort()[0] || "";
  }

  function formatInterviewWhen(interview) {
    return [formatDisplayDate(interview.date), interview.time].filter(Boolean).join(" at ") || "No date";
  }

  function setTaskMetric(name, value) {
    const element = document.querySelector(`[data-task-metric='${name}']`);
    if (element) element.textContent = String(value);
  }

  function formatLongDate(value) {
    const date = parseDateKey(value);
    return date.toLocaleDateString("en-US", {
      weekday: "long",
      month: "short",
      day: "numeric",
      year: "numeric"
    });
  }

  function formatHours(value) {
    return Number(value || 0).toLocaleString("en-US", {
      maximumFractionDigits: 1
    });
  }

  function formatDateInput(date) {
    const value = new Date(date);
    if (Number.isNaN(value.getTime())) return "";
    const year = value.getFullYear();
    const month = String(value.getMonth() + 1).padStart(2, "0");
    const day = String(value.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
  }

  function formatDisplayDate(value) {
    if (!value) return "";
    const parts = String(value).split("-");
    if (parts.length !== 3) return value;
    return `${parts[1]}/${parts[2]}/${parts[0]}`;
  }

  function parseDateKey(value) {
    const parts = String(value || "").split("-").map(Number);
    if (parts.length !== 3 || parts.some(Number.isNaN)) return new Date();
    return new Date(parts[0], parts[1] - 1, parts[2]);
  }

  function timeToMinutes(value) {
    const [hours, minutes] = String(value || "00:00").split(":").map(Number);
    return (hours || 0) * 60 + (minutes || 0);
  }

  function normalizeTimeValue(value) {
    const parts = String(value || "").split(":").map(Number);
    if (parts.length < 2 || parts.some(Number.isNaN)) return "";
    const hours = Math.max(0, Math.min(23, parts[0]));
    const minutes = Math.max(0, Math.min(59, parts[1]));
    return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}`;
  }

  function dateToMinutes(date) {
    return date.getHours() * 60 + date.getMinutes();
  }

  function formatMinutes(value) {
    const total = Math.max(0, Number(value) || 0);
    const hours = Math.floor(total / 60);
    const minutes = total % 60;
    return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}`;
  }

  function rangesOverlap(startA, endA, startB, endB) {
    return startA < endB && endA > startB;
  }

  function getDateKeyInZone(date) {
    const parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: TIME_ZONE,
      year: "numeric",
      month: "2-digit",
      day: "2-digit"
    }).formatToParts(date).reduce((result, part) => {
      result[part.type] = part.value;
      return result;
    }, {});
    return `${parts.year}-${parts.month}-${parts.day}`;
  }

  function getStatusClass(status) {
    return String(status || "").toLowerCase().replace(/[^a-z0-9]+/g, "-");
  }

  function clean(value) {
    return String(value || "").trim();
  }

  function csvCell(value) {
    return `"${String(value || "").replace(/"/g, '""')}"`;
  }

  function createId() {
    if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
    return `id-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function escapeHtml(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function escapeAttribute(value) {
    return escapeHtml(value).replace(/`/g, "&#96;");
  }
})();
