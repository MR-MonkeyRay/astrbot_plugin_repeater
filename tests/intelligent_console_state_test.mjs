import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

class ClassList {
  constructor() {
    this.names = new Set();
  }

  toggle(name, force) {
    const enabled = force === undefined ? !this.names.has(name) : Boolean(force);
    if (enabled) {
      this.names.add(name);
    } else {
      this.names.delete(name);
    }
    return enabled;
  }

  contains(name) {
    return this.names.has(name);
  }
}

class Element {
  constructor() {
    this.attributes = new Map();
    this.children = [];
    this.classList = new ClassList();
    this.className = "";
    this.dataset = {};
    this.disabled = false;
    this.hidden = false;
    this.listeners = new Map();
    this.textContent = "";
    this.value = "";
  }

  addEventListener(type, listener) {
    this.listeners.set(type, listener);
  }

  append(...children) {
    this.children.push(...children);
  }

  click() {
    return this.listeners.get("click")?.({ currentTarget: this });
  }

  replaceChildren(...children) {
    this.children = children;
  }

  setAttribute(name, value) {
    this.attributes.set(name, value);
  }

  getAttribute(name) {
    return this.attributes.get(name) || null;
  }

  focus() {}
}

const elements = new Map();
const elementFor = (selector) => {
  if (!elements.has(selector)) {
    elements.set(selector, new Element());
  }
  return elements.get(selector);
};

const selectorLists = new Map([
  ["[data-i18n]", []],
  ["[data-i18n-placeholder]", []],
  ["[data-i18n-aria-label]", []],
  [".range-tab", []],

  [".route-tab", []],
  [".view-tab", []],
  [".workspace-view", []],
]);

const astrbotRouteTab = new Element();
astrbotRouteTab.dataset.providerMode = "astrbot";
const manualRouteTab = new Element();
manualRouteTab.dataset.providerMode = "openai_compatible";
const configurationTab = new Element();
configurationTab.dataset.view = "configuration";
const testsTab = new Element();
testsTab.dataset.view = "tests";
const historyTab = new Element();
historyTab.dataset.view = "history";
const configurationView = new Element();
configurationView.id = "configuration-view";
const testsView = new Element();
testsView.id = "tests-view";
const historyView = new Element();
historyView.id = "history-view";
const history24hTab = new Element();
history24hTab.dataset.window = "24h";

selectorLists.set(".route-tab", [astrbotRouteTab, manualRouteTab]);
selectorLists.set(".view-tab", [configurationTab, testsTab, historyTab]);
selectorLists.set(".workspace-view", [configurationView, testsView, historyView]);
selectorLists.set(".range-tab", [history24hTab]);

globalThis.document = {
  createElement: () => new Element(),
  documentElement: new Element(),
  querySelector: elementFor,
  querySelectorAll: (selector) => selectorLists.get(selector) || [],
  title: "",
};

const configResponse = () => ({
  features: {
    intelligent_mute_enabled: false,
    intelligent_repeat_enabled: false,
  },
  history: { available: true },
  manual_api_base: "https://manual.example/v1",
  manual_api_key_configured: true,
  model: "manual-model",
  provider_catalog_available: false,
  provider_exists: true,
  provider_id: "",
  provider_mode: "openai_compatible",
  providers: [],
});

const historyRecords = [
  {
    id: 1,
    occurred_at_ms: 0,
    source: "runtime",
    kind: "repeat",
    outcome: "success",
    provider_id: "provider-a",
    model: "model-a",
    group_id: "group-a",
    latency_ms: 7,
    message_text: "first repeated message",
    prompt: "first prompt",
    completion: "first reply",
    repeat_user_count: 2,
  },
  {
    id: 2,
    occurred_at_ms: 1,
    source: "runtime",
    kind: "mute",
    outcome: "success",
    provider_id: "provider-b",
    model: "model-b",
    group_id: "group-b",
    mute_duration_seconds: 60,
    latency_ms: 8,
    message_text: "second repeated message",
    prompt: "second prompt",
    completion: "second reply",
    repeat_user_count: null,
  },
];
const historyRequests = [];
const historyClearPosts = [];
let historyCleared = false;

const configPosts = [];
globalThis.window = {
  AstrBotPluginPage: {
    apiGet: async (endpoint, params) => {

      if (endpoint === "intelligent-console/config") {
        return configResponse();
      }
      if (endpoint === "intelligent-console/history") {
        historyRequests.push(params);
        return {
          pagination: {
            page: params.page,
            total_pages: historyCleared ? 1 : 2,
          },
          range: null,
          records: historyCleared ? [] : historyRecords,
          summary: {},
        };
      }

      throw new Error(`Unexpected GET ${endpoint}`);
    },
    apiPost: async (endpoint, payload) => {
      if (endpoint === "intelligent-console/history/clear") {
        historyClearPosts.push(payload);
        historyCleared = true;
        return { deleted: 2 };
      }

      if (endpoint === "intelligent-console/test/repeat") {
        return {
          latency_ms: 7,
          model: "",
          provider_id: "provider-a",
          text: "provider result",
        };
      }
      assert.equal(endpoint, "intelligent-console/config");
      configPosts.push(payload);
      if (configPosts.length === 1) {
        throw new Error("clear failed");
      }
      return configResponse();
    },
    getLocale: () => "en-US",
    onContext: (callback) => callback({ isDark: false }),
    ready: async () => {},
    t: (_key, fallback) => fallback,
  },
  confirm: () => true,
};

const settle = async () => {
  for (let index = 0; index < 8; index += 1) {
    await new Promise(setImmediate);
  }
};

const source = readFileSync("pages/intelligent-console/app.js", "utf8");
eval(source);
await settle();
assert.equal(historyRequests[0].window, "24h");


assert.equal(configurationView.hidden, false);
assert.equal(testsView.hidden, true);
assert.equal(manualRouteTab.classList.contains("is-active"), true);
assert.equal(manualRouteTab.attributes.get("aria-checked"), "true");

testsTab.click();
assert.equal(testsView.hidden, false);
assert.equal(configurationView.hidden, true);
assert.equal(testsTab.attributes.get("aria-selected"), "true");

configurationTab.click();
configurationTab.listeners.get("keydown")({
  key: "ArrowRight",
  preventDefault() {},
});
assert.equal(testsView.hidden, false);
assert.equal(testsTab.tabIndex, 0);

testsTab.listeners.get("keydown")({
  key: "ArrowLeft",
  preventDefault() {},
});
assert.equal(configurationView.hidden, false);
assert.equal(configurationTab.tabIndex, 0);
astrbotRouteTab.click();
await settle();
assert.equal(astrbotRouteTab.classList.contains("is-active"), true);
assert.equal(elementFor("#astrbot-provider-field").hidden, false);
assert.equal(elementFor("#manual-api-base-field").hidden, true);
assert.equal(
  elementFor("#model-help").textContent,
  "In AstrBot mode, use this only to override the chat provider's model. Choose a candidate or enter a custom model ID.",
);

manualRouteTab.click();
await settle();
assert.equal(manualRouteTab.classList.contains("is-active"), true);
assert.equal(elementFor("#astrbot-provider-field").hidden, true);
assert.equal(elementFor("#manual-api-base-field").hidden, false);
assert.equal(
  elementFor("#model-help").textContent,
  "OpenAI-compatible direct mode requires a custom model ID and does not enumerate third-party models.",
);

elementFor("#clear-manual-api-key").click();
await settle();
assert.equal(configPosts.length, 1);
assert.equal(configPosts[0].manual_api_key, "");
assert.equal(elementFor("#config-feedback").textContent, "clear failed");

elementFor("#save-config").click();
await settle();
assert.equal(configPosts.length, 2);
assert.equal(Object.hasOwn(configPosts[1], "manual_api_key"), false);

elementFor("#manual-api-key-input").value = "replacement-key";
elementFor("#save-config").click();
await settle();
assert.equal(configPosts.length, 3);
elementFor("#test-repeat").click();
await settle();
assert.match(
  elementFor("#repeat-result").textContent,
  /Provider-selected model \(no custom model ID specified\)/,
);
assert.equal(configPosts[2].manual_api_key, "replacement-key");

const [firstRow, firstDetailRow, secondRow, secondDetailRow] = elementFor(
  "#history-rows",
).children;
assert.equal(firstDetailRow.hidden, true);
assert.equal(secondDetailRow.hidden, true);
assert.equal(firstDetailRow.children[0].children[0].children[0].children[1].textContent, "first repeated message");

firstRow.click();
assert.equal(firstRow.attributes.get("aria-expanded"), "true");
assert.equal(firstDetailRow.hidden, false);
assert.equal(secondDetailRow.hidden, true);

secondRow.click();
assert.equal(firstRow.attributes.get("aria-expanded"), "false");
assert.equal(firstDetailRow.hidden, true);
assert.equal(secondRow.attributes.get("aria-expanded"), "true");
assert.equal(secondDetailRow.hidden, false);
assert.equal(secondRow.children[5].textContent, "Group group-b");
const muteDurationDetail = secondDetailRow.children[0].children[0].children[3];
assert.equal(muteDurationDetail.children[0].textContent, "Mute duration");
assert.equal(muteDurationDetail.children[1].textContent, "Muted 60 s");

elementFor("#history-next").click();
await settle();
assert.equal(historyRequests[historyRequests.length - 1].page, 2);

elementFor("#clear-history").click();
await settle();
assert.deepEqual(historyClearPosts, [{}]);
assert.equal(historyRequests[historyRequests.length - 1].page, 1);
assert.equal(elementFor("#history-page").textContent, "Page 1 / 1");
assert.equal(elementFor("#history-prev").disabled, true);
assert.equal(elementFor("#history-next").disabled, true);
assert.equal(elementFor("#history-rows").children.length, 0);
assert.equal(elementFor("#metric-total").textContent, "0");
assert.equal(elementFor("#history-feedback").textContent, "Cleared 2 records.");
assert.equal(elementFor("#clear-history").disabled, false);
