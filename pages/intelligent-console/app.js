(() => {
  "use strict";

  const bridge = window.AstrBotPluginPage;
  const keyPrefix = "pages.intelligent-console";
  const state = {
    context: null,
    providerCatalogAvailable: true,
    providerCatalogLoading: false,
    providerCatalogRequestId: 0,
    providerMode: "astrbot",
    manualApiKeyConfigured: false,
    manualApiKeyClearRequested: false,
    configurationLoaded: false,
    history: {
      window: "day",
      kind: "all",
      page: 1,
      totalPages: 1,
      expandedRow: null,
    },
    modelRequestId: 0,
    historyRequestId: 0,
  };

  const elements = {
    viewTabs: document.querySelectorAll(".view-tab"),
    viewPanels: document.querySelectorAll(".workspace-view"),
    modeTabs: document.querySelectorAll(".route-tab"),
    modeHelp: document.querySelector("#provider-mode-help"),
    providerField: document.querySelector("#astrbot-provider-field"),
    provider: document.querySelector("#provider-select"),
    manualApiBaseField: document.querySelector("#manual-api-base-field"),
    manualApiKeyField: document.querySelector("#manual-api-key-field"),
    manualApiBase: document.querySelector("#manual-api-base-input"),
    manualApiKey: document.querySelector("#manual-api-key-input"),
    manualApiKeyStatus: document.querySelector("#manual-api-key-status"),
    clearManualApiKey: document.querySelector("#clear-manual-api-key"),
    model: document.querySelector("#model-input"),
    models: document.querySelector("#model-options"),
    modelHelp: document.querySelector("#model-help"),
    save: document.querySelector("#save-config"),
    configFeedback: document.querySelector("#config-feedback"),
    repeatRuntime: document.querySelector("#repeat-runtime-status"),
    muteRuntime: document.querySelector("#mute-runtime-status"),
    repeatTest: document.querySelector("#test-repeat"),
    muteTest: document.querySelector("#test-mute"),
    repeatResult: document.querySelector("#repeat-result"),
    muteResult: document.querySelector("#mute-result"),
    historyFeedback: document.querySelector("#history-feedback"),
    historyRange: document.querySelector("#history-range"),
    historyKind: document.querySelector("#history-kind"),
    historyRows: document.querySelector("#history-rows"),
    historyEmpty: document.querySelector("#history-empty"),
    historyPrevious: document.querySelector("#history-prev"),
    historyNext: document.querySelector("#history-next"),
    historyPage: document.querySelector("#history-page"),
    metrics: {
      total: document.querySelector("#metric-total"),
      repeat: document.querySelector("#metric-repeat"),
      mute: document.querySelector("#metric-mute"),
      success: document.querySelector("#metric-success"),
      fallback: document.querySelector("#metric-fallback"),
      failed: document.querySelector("#metric-failed"),
    },
  };


  function translate(key, fallback = "") {
    return bridge?.t?.(`${keyPrefix}.${key}`, fallback) ?? fallback;
  }

  function interpolate(template, values) {
    return Object.entries(values).reduce(
      (text, [name, value]) => text.replaceAll(`{${name}}`, String(value)),
      template,
    );
  }

  function formatError(error) {
    if (error instanceof Error && error.message) {
      return error.message;
    }
    return translate("common.unknown_error", "The operation failed. Please try again later.");
  }

  function setFeedback(element, message = "", tone = "") {
    element.textContent = message;
    element.className = `feedback${tone ? ` is-${tone}` : ""}`;
  }

  function setTestResult(element, message = "", isError = false) {
    element.textContent = message;
    element.classList.toggle("is-error", isError);
  }

  function applyTranslations() {
    document.documentElement.lang = bridge?.getLocale?.() || "zh-CN";
    document.title = translate("title", "Intelligent Copy Test");
    document.querySelectorAll("[data-i18n]").forEach((node) => {
      node.textContent = translate(node.dataset.i18n, node.textContent);
    });
    document.querySelectorAll("[data-i18n-placeholder]").forEach((node) => {
      node.placeholder = translate(node.dataset.i18nPlaceholder, node.placeholder);
    });
    document.querySelectorAll("[data-i18n-aria-label]").forEach((node) => {
      node.setAttribute(
        "aria-label",
        translate(node.dataset.i18nAriaLabel, node.getAttribute("aria-label") || ""),
      );
    });
  }

  async function apiGet(endpoint, params) {
    return bridge.apiGet(endpoint, params);
  }

  async function apiPost(endpoint, body) {
    return bridge.apiPost(endpoint, body);
  }

  function normalizeProviderMode(value) {
    return value === "openai_compatible" ? "openai_compatible" : "astrbot";
  }

  function isManualProviderMode() {
    return state.providerMode === "openai_compatible";
  }

  function syncProviderModeTabs() {
    elements.modeTabs.forEach((button) => {
      const selected = button.dataset.providerMode === state.providerMode;
      button.classList.toggle("is-active", selected);
      button.setAttribute("aria-checked", String(selected));
      button.tabIndex = selected ? 0 : -1;
    });
  }

  function viewFromLocation() {
    const view = window.location?.hash?.replace(/^#/, "");
    return ["configuration", "tests", "history"].includes(view)
      ? view
      : "configuration";
  }

  function selectView(view, { updateLocation = true } = {}) {
    const selectedView = ["configuration", "tests", "history"].includes(view)
      ? view
      : "configuration";
    state.activeView = selectedView;
    elements.viewTabs.forEach((button) => {
      const selected = button.dataset.view === selectedView;
      button.classList.toggle("is-active", selected);
      button.setAttribute("aria-selected", String(selected));
      button.tabIndex = selected ? 0 : -1;
    });
    elements.viewPanels.forEach((panel) => {
      const selected = panel.id === `${selectedView}-view`;
      panel.classList.toggle("is-active", selected);
      panel.hidden = !selected;
    });
    if (updateLocation && window.history?.replaceState && window.location) {
      window.history.replaceState(
        null,
        "",
        `${window.location.pathname || ""}${window.location.search || ""}#${selectedView}`,
      );
    }
  }

  function updateManualApiKeyStatus() {
    elements.manualApiKeyStatus.textContent = state.manualApiKeyConfigured
      ? translate(
        "configuration.manual_api_key.configured",
        "Saved; this page never displays the key.",
      )
      : translate(
        "configuration.manual_api_key.not_configured",
        "No API key is saved.",
      );
  }

  function setConfigurationControlsEnabled(enabled) {
    const editable = Boolean(enabled);
    const manualMode = isManualProviderMode();
    elements.providerField.hidden = manualMode;
    elements.manualApiBaseField.hidden = !manualMode;
    elements.manualApiKeyField.hidden = !manualMode;
    elements.modeTabs.forEach((button) => {
      button.disabled = !editable;
    });
    syncProviderModeTabs();
    elements.provider.disabled = (
      !editable || manualMode || !state.providerCatalogAvailable
    );
    elements.manualApiBase.disabled = !editable || !manualMode;
    elements.manualApiKey.disabled = !editable || !manualMode;
    elements.model.disabled = !editable;
    elements.clearManualApiKey.disabled = (
      !editable || !manualMode || !state.manualApiKeyConfigured
    );
    elements.save.disabled = (
      !editable || (!manualMode && state.providerCatalogLoading)
    );
    elements.modeHelp.textContent = manualMode
      ? translate(
        "configuration.mode.manual_help",
        "Connect directly to a compatible service without relying on the chat-provider catalog.",
      )
      : translate(
        "configuration.mode.astrbot_help",
        "Use an AstrBot-configured chat provider; a blank provider follows the triggering session.",
      );
    elements.model.placeholder = manualMode
      ? translate(
        "configuration.model.manual_placeholder",
        "A model ID is required in direct mode",
      )
      : translate(
        "configuration.model.placeholder",
        "Blank uses the AstrBot provider default model",
      );
    if (manualMode) {
      elements.modelHelp.textContent = translate(
        "configuration.model.manual_help",
        "Direct mode requires a model ID and does not enumerate third-party models.",
      );
    }
    updateManualApiKeyStatus();
  }

  function selectProviderMode(mode) {
    const providerMode = normalizeProviderMode(mode);
    if (providerMode === state.providerMode) {
      syncProviderModeTabs();
      return;
    }
    state.providerMode = providerMode;
    setConfigurationControlsEnabled(state.configurationLoaded);
    if (!state.configurationLoaded) {
      return;
    }
    if (isManualProviderMode()) {
      state.providerCatalogRequestId += 1;
      state.providerCatalogLoading = false;
      setFeedback(elements.configFeedback);
      void loadModels();
    } else {
      void loadAstrBotProviderCatalog();
    }
  }

  function setRuntimeStatus(element, enabled, activeKey, inactiveKey) {
    element.textContent = translate(activeKey, "") || translate(inactiveKey, "");
    element.classList.toggle("is-active", Boolean(enabled));
    element.classList.toggle("is-inactive", !enabled);
    element.textContent = enabled
      ? translate(activeKey, "Active")
      : translate(inactiveKey, "Off");
  }

  function createOption(value, label) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    return option;
  }

  function populateProviders(providers, selectedProvider, providerExists) {
    elements.provider.replaceChildren(
      createOption(
        "",
        translate("configuration.provider.empty", "Blank: follow triggering session"),
      ),
    );
    providers.forEach((provider) => {
      elements.provider.append(
        createOption(provider.id, provider.label || provider.id),
      );
    });
    if (selectedProvider && !providers.some(({ id }) => id === selectedProvider)) {
      const suffix = providerExists ? "" : " · unavailable";
      elements.provider.append(createOption(selectedProvider, `${selectedProvider}${suffix}`));
    }
    elements.provider.value = selectedProvider || "";
  }

  function populateModels(models) {
    elements.models.replaceChildren();
    models.forEach((model) => {
      const option = document.createElement("option");
      option.value = model;
      elements.models.append(option);
    });
  }

  async function loadAstrBotProviderCatalog() {
    const requestId = ++state.providerCatalogRequestId;
    state.providerCatalogLoading = true;
    state.providerCatalogAvailable = false;
    setConfigurationControlsEnabled(state.configurationLoaded);
    try {
      const data = await apiGet("intelligent-console/config", {
        include_provider_catalog: "1",
      });
      if (
        requestId !== state.providerCatalogRequestId
        || !state.configurationLoaded
        || isManualProviderMode()
      ) {
        return;
      }
      const providers = Array.isArray(data.providers) ? data.providers : [];
      const selectedProvider = elements.provider.value || data.provider_id || "";
      state.providerCatalogLoading = false;
      state.providerCatalogAvailable = Boolean(data.provider_catalog_available);
      populateProviders(
        providers,
        selectedProvider,
        !selectedProvider || providers.some(({ id }) => id === selectedProvider),
      );
      setConfigurationControlsEnabled(true);
      if (!state.providerCatalogAvailable) {
        populateModels([]);
        setFeedback(
          elements.configFeedback,
          translate("common.provider_unavailable", "The chat-provider list is unavailable."),
          "error",
        );
        return;
      }
      setFeedback(elements.configFeedback);
      await loadModels();
    } catch (error) {
      if (
        requestId !== state.providerCatalogRequestId
        || !state.configurationLoaded
        || isManualProviderMode()
      ) {
        return;
      }
      state.providerCatalogLoading = false;
      state.providerCatalogAvailable = false;
      populateModels([]);
      setConfigurationControlsEnabled(true);
      setFeedback(elements.configFeedback, formatError(error), "error");
    }
  }

  async function loadModels() {
    const requestId = ++state.modelRequestId;
    populateModels([]);
    if (isManualProviderMode()) {
      elements.modelHelp.textContent = translate(
        "configuration.model.manual_help",
        "Direct mode requires a model ID and does not enumerate third-party models.",
      );
      return;
    }
    const providerId = elements.provider.value;
    if (!providerId) {
      elements.modelHelp.textContent = translate(
        "configuration.provider.help",
        "A blank provider follows the triggering group-message session and cannot be tested from this page.",
      );
      return;
    }
    elements.modelHelp.textContent = translate(
      "configuration.model.loading",
      "Loading model candidates…",
    );
    try {
      const data = await apiGet("intelligent-console/models", {
        provider_id: providerId,
      });
      if (requestId !== state.modelRequestId) {
        return;
      }
      populateModels(Array.isArray(data.models) ? data.models : []);
      elements.modelHelp.textContent = translate(
        "configuration.model.help",
        "Choose a candidate or enter a custom model ID that was not enumerated.",
      );
    } catch (error) {
      if (requestId !== state.modelRequestId) {
        return;
      }
      elements.modelHelp.textContent = translate(
        "configuration.model.unavailable",
        "Model candidates are unavailable; a custom model ID can still be entered.",
      );
      setFeedback(elements.configFeedback, formatError(error), "error");
    }
  }

  function renderRuntimeStatus(features) {
    setRuntimeStatus(
      elements.repeatRuntime,
      features?.intelligent_repeat_enabled,
      "runtime.repeat_on",
      "runtime.repeat_off",
    );
    setRuntimeStatus(
      elements.muteRuntime,
      features?.intelligent_mute_enabled,
      "runtime.mute_on",
      "runtime.mute_off",
    );
  }

  async function loadConfiguration() {
    const previousState = {
      configurationLoaded: state.configurationLoaded,
      providerCatalogAvailable: state.providerCatalogAvailable,
      providerMode: state.providerMode,
      manualApiKeyConfigured: state.manualApiKeyConfigured,
    };
    const restorePreviousConfigurationState = () => {
      state.configurationLoaded = previousState.configurationLoaded;
      state.providerCatalogAvailable = previousState.providerCatalogAvailable;
      state.providerMode = previousState.providerMode;
      state.manualApiKeyConfigured = previousState.manualApiKeyConfigured;
      state.providerCatalogLoading = false;
      setConfigurationControlsEnabled(state.configurationLoaded);
    };
    state.providerCatalogRequestId += 1;
    state.providerCatalogLoading = false;
    state.configurationLoaded = false;
    setConfigurationControlsEnabled(false);
    setFeedback(
      elements.configFeedback,
      translate("common.loading", "Loading…"),
    );
    try {
      const data = await apiGet("intelligent-console/config");
      renderRuntimeStatus(data.features || {});
      if (data.history?.available === false) {
        setFeedback(
          elements.historyFeedback,
          data.history.message || translate("history.unavailable"),
          "error",
        );
      }
      state.providerCatalogAvailable = Boolean(data.provider_catalog_available);
      state.providerMode = normalizeProviderMode(data.provider_mode);
      state.manualApiKeyConfigured = Boolean(data.manual_api_key_configured);
      state.manualApiKeyClearRequested = false;
      populateProviders(
        Array.isArray(data.providers) ? data.providers : [],
        data.provider_id || "",
        Boolean(data.provider_exists),
      );
      elements.manualApiBase.value = data.manual_api_base || "";
      elements.manualApiKey.value = "";
      elements.model.value = data.model || "";
      state.configurationLoaded = true;
      setConfigurationControlsEnabled(true);
      if (isManualProviderMode()) {
        populateModels([]);
        setFeedback(elements.configFeedback);
      } else if (!state.providerCatalogAvailable) {
        setFeedback(
          elements.configFeedback,
          translate("common.provider_unavailable", "The chat-provider list is unavailable."),
          "error",
        );
      } else {
        setFeedback(elements.configFeedback);
        await loadModels();
      }
      return true;
    } catch (error) {
      restorePreviousConfigurationState();
      setFeedback(elements.configFeedback, formatError(error), "error");
      return false;
    }
  }

  async function saveConfiguration() {
    if (!state.configurationLoaded) {
      setFeedback(
        elements.configFeedback,
        translate(
          "common.configuration_not_loaded",
          "Load the saved configuration before saving changes.",
        ),
        "error",
      );
      return;
    }
    setConfigurationControlsEnabled(false);
    setFeedback(
      elements.configFeedback,
      translate("configuration.saving", "Saving…"),
    );
    try {
      const payload = {
        provider_mode: state.providerMode,
        provider_id: elements.provider.value.trim(),
        manual_api_base: elements.manualApiBase.value.trim(),
        model: elements.model.value.trim(),
      };
      if (isManualProviderMode()) {
        const manualApiKey = elements.manualApiKey.value.trim();
        if (manualApiKey) {
          payload.manual_api_key = manualApiKey;
        } else if (state.manualApiKeyClearRequested) {
          payload.manual_api_key = "";
        }
      }
      const data = await apiPost("intelligent-console/config", payload);
      state.providerMode = data.provider_mode === "openai_compatible"
        ? "openai_compatible"
        : "astrbot";
      state.manualApiKeyConfigured = Boolean(data.manual_api_key_configured);
      state.manualApiKeyClearRequested = false;
      syncProviderModeTabs();
      elements.provider.value = data.provider_id || "";
      elements.manualApiBase.value = data.manual_api_base || "";
      elements.manualApiKey.value = "";
      elements.model.value = data.model || "";
      if (!await loadConfiguration()) {
        return;
      }
      setFeedback(
        elements.configFeedback,
        translate("configuration.saved", "Configuration saved."),
        "success",
      );
    } catch (error) {
      state.manualApiKeyClearRequested = false;
      setFeedback(elements.configFeedback, formatError(error), "error");
    } finally {
      setConfigurationControlsEnabled(state.configurationLoaded);
    }
  }

  function clearManualApiKey() {
    if (!state.configurationLoaded || !isManualProviderMode()) {
      return;
    }
    const confirmed = typeof window.confirm !== "function" || window.confirm(
      translate(
        "configuration.manual_api_key.clear_confirm",
        "Clear the saved API key? This saves immediately and cannot be undone.",
      ),
    );
    if (!confirmed) {
      return;
    }
    state.manualApiKeyClearRequested = true;
    elements.manualApiKey.value = "";
    void saveConfiguration();
  }

  async function runTest(kind) {
    const button = kind === "repeat" ? elements.repeatTest : elements.muteTest;
    const result = kind === "repeat" ? elements.repeatResult : elements.muteResult;
    button.disabled = true;
    setTestResult(
      result,
      translate("tests.running", "Generating test copy…"),
    );
    try {
      const data = await apiPost(`intelligent-console/test/${kind}`, {});
      const model = data.model || translate("configuration.model.placeholder", "default");
      setTestResult(
        result,
        interpolate(
          translate("tests.result", "{provider} · {model} · {latency} ms\n{text}"),
          {
            provider: data.provider_id || translate("history.not_available", "—"),
            model,
            latency: data.latency_ms ?? 0,
            text: data.text || "",
          },
        ),
      );
      await loadHistory();
    } catch (error) {
      setTestResult(
        result,
        interpolate(
          translate("tests.failed", "Test failed: {message}"),
          { message: formatError(error) },
        ),
        true,
      );
      await loadHistory();
    } finally {
      button.disabled = false;
    }
  }

  function formatTimestamp(value) {
    if (!Number.isFinite(value)) {
      return translate("history.not_available", "—");
    }
    return new Intl.DateTimeFormat(bridge?.getLocale?.() || "zh-CN", {
      dateStyle: "short",
      timeStyle: "medium",
    }).format(new Date(value));
  }

  function renderSummary(summary) {
    Object.entries(elements.metrics).forEach(([name, element]) => {
      element.textContent = String(summary?.[name] ?? 0);
    });
  }

  function contextLabel(record) {
    const values = [];
    if (record.group_id) {
      values.push(
        interpolate(translate("history.group", "Group {group}"), {
          group: record.group_id,
        }),
      );
    }
    if (Number.isInteger(record.mute_duration_seconds)) {
      values.push(
        interpolate(translate("history.duration", "Muted {duration} s"), {
          duration: record.mute_duration_seconds,
        }),
      );
    }
    return values.join(" · ") || translate("history.not_available", "—");
  }

  function appendCell(row, content, className = "") {
    const cell = document.createElement("td");
    if (className) {
      cell.className = className;
    }
    cell.textContent = content;
    row.append(cell);
    return cell;
  }

  function historyDetailValue(value) {
    if (value === null || value === undefined || value === "") {
      return translate("history.not_available", "—");
    }
    return String(value);
  }

  function appendHistoryDetail(
    details,
    labelKey,
    fallbackLabel,
    value,
    wide = false,
  ) {
    const detail = document.createElement("div");
    detail.className = `history-detail${wide ? " history-detail-wide" : ""}`;
    const label = document.createElement("dt");
    label.textContent = translate(labelKey, fallbackLabel);
    const content = document.createElement("dd");
    content.textContent = historyDetailValue(value);
    detail.append(label, content);
    details.append(detail);
  }

  function setHistoryRowExpanded(row, detailRow, expanded) {
    row.classList.toggle("is-expanded", expanded);
    row.setAttribute("aria-expanded", String(expanded));
    detailRow.hidden = !expanded;
  }

  function toggleHistoryRow(row, detailRow) {
    const expandedRow = state.history.expandedRow;
    const isCurrentRow = expandedRow?.row === row;
    if (expandedRow) {
      setHistoryRowExpanded(expandedRow.row, expandedRow.detailRow, false);
    }
    state.history.expandedRow = null;
    if (!isCurrentRow) {
      setHistoryRowExpanded(row, detailRow, true);
      state.history.expandedRow = { row, detailRow };
    }
  }

  function renderHistoryRows(records) {
    state.history.expandedRow = null;
    elements.historyRows.replaceChildren();
    elements.historyEmpty.hidden = records.length !== 0;
    records.forEach((record, index) => {
      const recordId = record.id ?? `page-${index}`;
      const detailId = `history-detail-${recordId}`;
      const row = document.createElement("tr");
      row.className = "history-record-row";
      row.tabIndex = 0;
      row.setAttribute("role", "button");
      row.setAttribute("aria-controls", detailId);
      row.setAttribute("aria-expanded", "false");
      row.setAttribute(
        "aria-label",
        translate("history.detail.toggle", "Toggle record details"),
      );
      appendCell(row, formatTimestamp(record.occurred_at_ms), "cell-meta");
      appendCell(
        row,
        translate(`history.source.${record.source}`, record.source || ""),
      );
      appendCell(
        row,
        translate(`history.action.${record.kind}`, record.kind || ""),
      );
      const outcome = document.createElement("td");
      const outcomeLabel = document.createElement("span");
      outcomeLabel.className = `outcome outcome-${record.outcome}`;
      const outcomeText = translate(
        `history.outcome.${record.outcome}`,
        record.outcome || "",
      );
      outcomeLabel.textContent = record.failure_code
        ? `${outcomeText} · ${record.failure_code}`
        : outcomeText;
      outcome.append(outcomeLabel);
      row.append(outcome);
      appendCell(
        row,
        `${record.provider_id || translate("history.not_available", "—")} / ${record.model || translate("history.not_available", "—")}`,
        "cell-meta",
      );
      appendCell(row, contextLabel(record), "cell-meta");
      appendCell(row, `${record.latency_ms ?? 0} ms`, "cell-meta");

      const detailRow = document.createElement("tr");
      detailRow.className = "history-detail-row";
      detailRow.id = detailId;
      detailRow.hidden = true;
      const detailCell = document.createElement("td");
      detailCell.className = "history-detail-cell";
      detailCell.colSpan = 7;
      const details = document.createElement("dl");
      details.className = "history-details";
      appendHistoryDetail(
        details,
        "history.detail.message_text",
        "Repeated content",
        record.message_text,
        true,
      );
      appendHistoryDetail(
        details,
        "history.detail.prompt",
        "LLM request",
        record.prompt,
        true,
      );
      appendHistoryDetail(
        details,
        "history.detail.completion",
        "LLM reply",
        record.completion,
        true,
      );
      appendHistoryDetail(
        details,
        "history.detail.repeat_user_count",
        "Repeat users",
        record.repeat_user_count,
      );
      detailCell.append(details);
      detailRow.append(detailCell);

      const toggle = () => toggleHistoryRow(row, detailRow);
      row.addEventListener("click", toggle);
      row.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") {
          return;
        }
        event.preventDefault();
        toggle();
      });
      elements.historyRows.append(row, detailRow);
    });
  }

  function renderHistoryRange(range) {
    if (!range) {
      elements.historyRange.textContent = "";
      return;
    }
    elements.historyRange.textContent = interpolate(
      translate("history.range", "{start} — {end} ({timezone})"),
      {
        start: range.start_display || formatTimestamp(range.start_at_ms),
        end: range.end_display || formatTimestamp(range.end_at_ms),
        timezone: range.timezone || "UTC",
      },
    );
  }

  function updatePagination(pagination) {
    state.history.totalPages = Math.max(1, Number(pagination?.total_pages) || 1);
    state.history.page = Number(pagination?.page) || 1;
    elements.historyPage.textContent = interpolate(
      translate("history.pagination.page", "Page {page} / {total}"),
      { page: state.history.page, total: state.history.totalPages },
    );
    elements.historyPrevious.disabled = state.history.page <= 1;
    elements.historyNext.disabled = state.history.page >= state.history.totalPages;
  }

  async function loadHistory() {
    const requestId = ++state.historyRequestId;
    setFeedback(
      elements.historyFeedback,
      translate("history.loading", "Loading records…"),
    );
    try {
      const data = await apiGet("intelligent-console/history", {
        window: state.history.window,
        kind: state.history.kind,
        page: state.history.page,
        page_size: 50,
      });
      if (requestId !== state.historyRequestId) {
        return;
      }
      renderSummary(data.summary || {});
      renderHistoryRange(data.range);
      renderHistoryRows(Array.isArray(data.records) ? data.records : []);
      updatePagination(data.pagination || {});
      setFeedback(elements.historyFeedback);
    } catch (error) {
      if (requestId !== state.historyRequestId) {
        return;
      }
      renderSummary({});
      renderHistoryRange(null);
      renderHistoryRows([]);
      updatePagination({ page: 1, total_pages: 1 });
      setFeedback(elements.historyFeedback, formatError(error), "error");
    }
  }

  function selectWindow(window) {
    state.history.window = window;
    state.history.page = 1;
    document.querySelectorAll(".range-tab").forEach((button) => {
      const selected = button.dataset.window === window;
      button.classList.toggle("is-active", selected);
      button.setAttribute("aria-pressed", String(selected));
    });
    void loadHistory();
  }

  function bindViewTabs() {
    const tabs = Array.from(elements.viewTabs);
    tabs.forEach((button, index) => {
      button.addEventListener("click", () => selectView(button.dataset.view));
      button.addEventListener("keydown", (event) => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
          return;
        }
        event.preventDefault();
        const nextIndex = event.key === "Home"
          ? 0
          : event.key === "End"
            ? tabs.length - 1
            : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
        const nextTab = tabs[nextIndex];
        nextTab.focus?.();
        selectView(nextTab.dataset.view);
      });
    });
  }

  function bindModeTabs() {
    const tabs = Array.from(elements.modeTabs);
    tabs.forEach((button, index) => {
      button.addEventListener("click", () => selectProviderMode(button.dataset.providerMode));
      button.addEventListener("keydown", (event) => {
        if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) {
          return;
        }
        event.preventDefault();
        const nextIndex = event.key === "Home"
          ? 0
          : event.key === "End"
            ? tabs.length - 1
            : (index + (["ArrowRight", "ArrowDown"].includes(event.key) ? 1 : -1) + tabs.length) % tabs.length;
        const nextTab = tabs[nextIndex];
        nextTab.focus?.();
        selectProviderMode(nextTab.dataset.providerMode);
      });
    });
  }

  function bindControls() {
    bindViewTabs();
    bindModeTabs();
    elements.provider.addEventListener("change", () => {
      if (!isManualProviderMode()) {
        void loadModels();
      }
    });
    elements.clearManualApiKey.addEventListener("click", clearManualApiKey);
    elements.save.addEventListener("click", () => {
      void saveConfiguration();
    });
    elements.repeatTest.addEventListener("click", () => {
      void runTest("repeat");
    });
    elements.muteTest.addEventListener("click", () => {
      void runTest("mute");
    });
    document.querySelectorAll(".range-tab").forEach((button) => {
      button.addEventListener("click", () => selectWindow(button.dataset.window));
    });
    elements.historyKind.addEventListener("change", () => {
      state.history.kind = elements.historyKind.value;
      state.history.page = 1;
      void loadHistory();
    });
    elements.historyPrevious.addEventListener("click", () => {
      if (state.history.page > 1) {
        state.history.page -= 1;
        void loadHistory();
      }
    });
    elements.historyNext.addEventListener("click", () => {
      if (state.history.page < state.history.totalPages) {
        state.history.page += 1;
        void loadHistory();
      }
    });
  }

  async function start() {
    applyTranslations();
    selectView(viewFromLocation(), { updateLocation: false });
    setConfigurationControlsEnabled(false);
    bindControls();
    window.addEventListener?.("hashchange", () => {
      selectView(viewFromLocation(), { updateLocation: false });
    });
    if (!bridge) {
      setFeedback(
        elements.configFeedback,
        translate("common.unknown_error", "The operation failed. Please try again later."),
        "error",
      );
      return;
    }
    bridge.onContext((context) => {
      state.context = context;
      if (typeof context?.isDark === "boolean") {
        document.documentElement.dataset.theme = context.isDark ? "dark" : "light";
      }
      applyTranslations();
      renderRuntimeStatus({
        intelligent_repeat_enabled: elements.repeatRuntime.classList.contains("is-active"),
        intelligent_mute_enabled: elements.muteRuntime.classList.contains("is-active"),
      });
    });
    try {
      await bridge.ready();
      await Promise.all([loadConfiguration(), loadHistory()]);
    } catch (error) {
      setFeedback(elements.configFeedback, formatError(error), "error");
    }
  }

  void start();
})();
