(() => {
  "use strict";

  const bridge = window.AstrBotPluginPage;
  const keyPrefix = "pages.intelligent-console";
  const state = {
    context: null,
    providerCatalogAvailable: true,
    configurationLoaded: false,
    history: {
      window: "day",
      kind: "all",
      page: 1,
      totalPages: 1,
    },
    modelRequestId: 0,
    historyRequestId: 0,
  };

  const elements = {
    provider: document.querySelector("#provider-select"),
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
  }

  async function apiGet(endpoint, params) {
    return bridge.apiGet(endpoint, params);
  }

  async function apiPost(endpoint, body) {
    return bridge.apiPost(endpoint, body);
  }

  function setConfigurationControlsEnabled(enabled) {
    const editable = Boolean(enabled);
    elements.provider.disabled = !editable;
    elements.model.disabled = !editable;
    elements.save.disabled = !editable;
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

  async function loadModels() {
    const providerId = elements.provider.value;
    const requestId = ++state.modelRequestId;
    populateModels([]);
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
    const wasConfigurationLoaded = state.configurationLoaded;
    const wasProviderCatalogAvailable = state.providerCatalogAvailable;
    const restorePreviousConfigurationState = () => {
      state.configurationLoaded = wasConfigurationLoaded;
      state.providerCatalogAvailable = wasProviderCatalogAvailable;
      setConfigurationControlsEnabled(
        state.configurationLoaded && state.providerCatalogAvailable,
      );
    };
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
      const providerCatalogAvailable = Boolean(data.provider_catalog_available);
      if (!providerCatalogAvailable) {
        restorePreviousConfigurationState();
        setFeedback(
          elements.configFeedback,
          translate("common.provider_unavailable", "The chat-provider list is unavailable."),
          "error",
        );
        return false;
      }
      state.providerCatalogAvailable = providerCatalogAvailable;
      populateProviders(
        Array.isArray(data.providers) ? data.providers : [],
        data.provider_id || "",
        Boolean(data.provider_exists),
      );
      elements.model.value = data.model || "";
      setFeedback(elements.configFeedback);
      await loadModels();
      state.configurationLoaded = true;
      setConfigurationControlsEnabled(true);
      return true;
    } catch (error) {
      restorePreviousConfigurationState();
      setFeedback(elements.configFeedback, formatError(error), "error");
      return false;
    }
  }

  async function saveConfiguration() {
    if (!state.configurationLoaded || !state.providerCatalogAvailable) {
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
      const data = await apiPost("intelligent-console/config", {
        provider_id: elements.provider.value.trim(),
        model: elements.model.value.trim(),
      });
      elements.provider.value = data.provider_id || "";
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
      setFeedback(elements.configFeedback, formatError(error), "error");
    } finally {
      setConfigurationControlsEnabled(
        state.configurationLoaded && state.providerCatalogAvailable,
      );
    }
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

  function renderHistoryRows(records) {
    elements.historyRows.replaceChildren();
    elements.historyEmpty.hidden = records.length !== 0;
    records.forEach((record) => {
      const row = document.createElement("tr");
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
      elements.historyRows.append(row);
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
      button.setAttribute("aria-selected", String(selected));
    });
    void loadHistory();
  }

  function bindControls() {
    elements.provider.addEventListener("change", () => {
      void loadModels();
    });
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
    setConfigurationControlsEnabled(false);
    bindControls();
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
