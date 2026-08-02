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
  [".range-tab", []],
]);

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

const configPosts = [];
globalThis.window = {
  AstrBotPluginPage: {
    apiGet: async (endpoint) => {
      if (endpoint === "intelligent-console/config") {
        return configResponse();
      }
      if (endpoint === "intelligent-console/history") {
        return {
          pagination: { page: 1, total_pages: 1 },
          range: null,
          records: [],
          summary: {},
        };
      }
      throw new Error(`Unexpected GET ${endpoint}`);
    },
    apiPost: async (endpoint, payload) => {
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
assert.equal(configPosts[2].manual_api_key, "replacement-key");
