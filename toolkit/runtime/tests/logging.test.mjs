// The Node logging convention, same as test_logging.py. Run: node --test toolkit/runtime/tests
import assert from "node:assert/strict";
import { test } from "node:test";
import { EventEmitter } from "node:events";

process.env.LOG_LEVEL = "debug";
const { log, annexRequest, newRequestId } = await import("../annex_runtime.js");

function capture(fn) {
  const lines = [];
  const original = process.stdout.write;
  process.stdout.write = (chunk) => { lines.push(String(chunk)); return true; };
  try { fn(); } finally { process.stdout.write = original; }
  return lines.join("").split("\n").filter(Boolean).map((l) => JSON.parse(l));
}

function fakeRequest(headers = {}, path = "/") {
  const h = Object.fromEntries(Object.entries(headers).map(([k, v]) => [k.toLowerCase(), v]));
  const req = { get: (n) => h[n.toLowerCase()], method: "GET", path };
  const res = Object.assign(new EventEmitter(), { statusCode: 200, headers: {}, setHeader(k, v) { this.headers[k] = v; } });
  return { req, res };
}

test("every line is JSON with ts, level and msg", () => {
  const [line] = capture(() => log.info("report written", { rows: 12 }));
  assert.equal(line.level, "info"); assert.equal(line.msg, "report written"); assert.equal(line.rows, 12);
  assert.ok(!Number.isNaN(Date.parse(line.ts)));
});

test("levels debug, info, warn, error", () => {
  const lines = capture(() => { log.debug("a"); log.info("b"); log.warn("c"); log.error("d"); });
  assert.deepEqual(lines.map((l) => l.level), ["debug", "info", "warn", "error"]);
});

test("a field can't overwrite the required ones, and errors are stringified", () => {
  const [line] = capture(() => log.info("real", { level: "fake", user: "fake", cause: new TypeError("boom") }));
  assert.equal(line.level, "info"); assert.equal(line.level_, "fake"); assert.equal(line.user_, "fake");
  assert.equal(line.cause, "TypeError: boom");
});

test("the middleware sets the header, request_id and user on lines inside, and logs one access line", () => {
  const { req, res } = fakeRequest({ "X-Request-Id": "abcd1234efgh", "Remote-User": "cm" }, "/search");
  const lines = capture(() => {
    annexRequest(req, res, () => log.info("handling"));
    res.statusCode = 201; res.emit("finish");
  });
  assert.equal(res.headers["X-Request-Id"], "abcd1234efgh");
  assert.equal(lines.length, 2);
  assert.deepEqual([lines[0].request_id, lines[0].user], ["abcd1234efgh", "cm"]);
  assert.equal(lines[1].msg, "request"); assert.equal(lines[1].path, "/search"); assert.equal(lines[1].status, 201);
  assert.equal(lines[1].request_id, "abcd1234efgh"); assert.equal(typeof lines[1].ms, "number");
});

test("outside a request there is no request_id", () => {
  const [line] = capture(() => log.info("startup"));
  assert.equal(line.request_id, undefined);
});

test("an unsafe or missing request id is replaced by 16 hex characters", () => {
  for (const bad of ["", "short", "has spaces in it", "x".repeat(65)]) {
    const { req, res } = fakeRequest(bad ? { "X-Request-Id": bad } : {});
    capture(() => annexRequest(req, res, () => {}));
    assert.match(res.headers["X-Request-Id"], /^[0-9a-f]{16}$/, bad);
  }
  assert.match(newRequestId(), /^[0-9a-f]{16}$/);
});

test("health checks are logged at debug level only", () => {
  const { req, res } = fakeRequest({}, "/healthz");
  const [line] = capture(() => { annexRequest(req, res, () => {}); res.emit("finish"); });
  assert.equal(line.level, "debug");
});

// --- browser errors (M2.3) -------------------------------------------------------------------------------
const { reportBrowserError, browserErrorsJs } = await import("../annex_runtime.js");

test("a browser error report is one error line with source=browser", () => {
  const get = (n) => ({ "remote-user": "sam" })[n];
  const [line] = capture(() => assert.equal(reportBrowserError(get, JSON.stringify({ msg: "x is not defined", url: "/p" })), 204));
  assert.equal(line.level, "error"); assert.equal(line.msg, "browser error: x is not defined"); assert.equal(line.source, "browser");
});

test("bad, big and too many reports", () => {
  const get = (n) => ({ "remote-user": "limit-user" })[n];
  assert.equal(reportBrowserError(get, "nope"), 400);
  assert.equal(reportBrowserError(get, "[1]"), 400);
  assert.equal(reportBrowserError(get, "x".repeat(9000)), 413);
  const codes = [];
  capture(() => { for (let i = 0; i < 12; i++) codes.push(reportBrowserError(get, '{"msg":"e"}')); });
  assert.deepEqual(codes, [...Array(10).fill(204), 429, 429]);
});

test("the browser script reports errors and rejections to /api/_errors, without the query string, at most 5 a page", () => {
  const listeners = {}, calls = [];
  const sandbox = { window: { addEventListener: (t, f) => { listeners[t] = f; } }, location: { pathname: "/page", search: "?q=secret" },
    fetch: (url, opts) => { calls.push([url, JSON.parse(opts.body)]); return Promise.resolve(); }, String, JSON };
  new Function(...Object.keys(sandbox), browserErrorsJs())(...Object.values(sandbox));
  listeners.error({ message: "boom", error: { stack: "Error: boom\n at x" } });
  listeners.unhandledrejection({ reason: new Error("later") });
  for (let i = 0; i < 6; i++) listeners.error({ message: `e${i}` });
  assert.equal(calls.length, 5);
  assert.equal(calls[0][0], "/api/_errors"); assert.deepEqual(Object.keys(calls[0][1]).sort(), ["msg", "stack", "url"]);
  assert.equal(calls[0][1].url, "/page"); assert.equal(calls[1][1].msg, "later");
  assert.ok(!JSON.stringify(calls).includes("secret"));
});
