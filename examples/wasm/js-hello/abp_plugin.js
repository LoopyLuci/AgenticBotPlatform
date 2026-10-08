// An ABP plugin written in JavaScript, compiled to a component by jco (componentize-js).
//
// The world (wit/abp-plugin.wit) is the same for every language. This module exports the
// world's `guest` interface - `describe` and `invoke` - and reaches the host through the
// `abp:plugin/host` import. Build it with:
//
//   npm install --save-dev @bytecodealliance/jco @bytecodealliance/componentize-js   # per-user, no admin
//   npx jco componentize abp_plugin.js --wit ../../../wit --world-name plugin --disable http fetch-event \
//       -o abp_js_hello.wasm
//
// `--disable http fetch-event` matters: componentize-js otherwise imports wasi:http for `fetch`, which ABP does
// not provide (a plugin reaches the network only through the host's checked `http-fetch`), and the component
// would not load.
//
// What the types become in JavaScript: a WIT record is a plain object with camelCase fields, a
// `list` is an array, and `result<string, string>` is a returned string for `ok` and a thrown
// value for `err`.
//
// Like the other examples it asks for nothing - no preopens, no network - so it installs
// without a conversation.

import { log } from "abp:plugin/host@0.1.0";

// The one tool this plugin offers, with the JSON Schema for its arguments. ABP hands the
// schema to the model verbatim, so it is a real schema rather than prose.
const GREET_SCHEMA = JSON.stringify({
  type: "object",
  properties: {
    name: { type: "string", description: "who to greet" },
    greeting: { type: "string", description: "the greeting to use; defaults to Hello" },
  },
  required: ["name"],
});

export const guest = {
  // Who this plugin is and what it can do. ABP reads this at install time, before anything is
  // approved.
  describe() {
    return {
      name: "js-hello",
      version: "1.0.0",
      description: "Greets someone. The same plugin as the Rust example, in JavaScript.",
      tools: [
        {
          name: "greet",
          description: "Greet someone by name, optionally with a greeting of your choosing",
          schema: GREET_SCHEMA,
        },
      ],
      preopens: [],
      network: [],
    };
  },

  // Run one tool: the result as JSON, or a thrown message the caller should see.
  invoke(tool, argsJson) {
    if (tool !== "greet") {
      throw `this plugin has one tool, greet; ${JSON.stringify(tool)} is not one of them`;
    }
    let args;
    try {
      args = JSON.parse(argsJson);
    } catch (e) {
      throw `the arguments are not JSON: ${e}`;
    }
    if (args === null || typeof args !== "object" || Array.isArray(args)) {
      throw "greet's arguments must be an object";
    }
    if (typeof args.name !== "string" || args.name === "") {
      throw 'greet needs a "name" string';
    }
    const greeting = typeof args.greeting === "string" && args.greeting !== "" ? args.greeting : "Hello";
    log("info", `greeted ${args.name}`);
    return JSON.stringify({ greeting: `${greeting}, ${args.name}!` });
  },
};
