// The cross-site check for Node, same cases as test_csrf.py. Run: node --test toolkit/runtime/tests
import assert from "node:assert/strict";
import { test } from "node:test";

const { csrfViolation, csrfGuard } = await import("../annex_runtime.js");

const getter = (headers) => { const h = Object.fromEntries(Object.entries(headers).map(([k, v]) => [k.toLowerCase(), v])); return (n) => h[n.toLowerCase()]; };
const HOST = { host: "myapp.annex.lan:8443" };
const v = (method, headers) => csrfViolation(method, getter({ ...HOST, ...headers }));

test.beforeEach(() => { delete process.env.ANNEX_CSRF; });

test("requests without browser headers pass, also webhook delivery", () => {
  for (const m of ["POST", "PUT", "PATCH", "DELETE"]) assert.equal(v(m, {}), null);
  assert.equal(v("POST", { "content-type": "application/json", "x-annex-hook": "trello", "x-annex-hook-token": "t" }), null);
});

test("safe methods are never checked", () => {
  assert.equal(v("GET", { "sec-fetch-site": "cross-site", origin: "https://evil.example" }), null);
  assert.equal(v("HEAD", { "sec-fetch-site": "cross-site" }), null);
});

test("Sec-Fetch-Site must be same-origin or none", () => {
  for (const ok of ["same-origin", "none", "Same-Origin"]) assert.equal(v("POST", { "sec-fetch-site": ok }), null);
  for (const bad of ["cross-site", "same-site", "garbage"]) assert.match(v("POST", { "sec-fetch-site": bad }), new RegExp(bad));
});

test("Origin must be the app's own host", () => {
  assert.equal(v("POST", { origin: "https://myapp.annex.lan:8443" }), null);
  assert.equal(v("POST", { origin: "https://MyApp.annex.lan:8443/" }), null);
  assert.ok(v("POST", { origin: "https://other.annex.lan:8443" }));
  assert.ok(v("POST", { origin: "https://myapp.annex.lan" }));
  assert.ok(v("POST", { origin: "null" }));
  assert.ok(v("POST", { origin: "" }));
});

test("X-Forwarded-Host counts as the own host", () => {
  const hdrs = { host: "app:8080", "x-forwarded-host": "myapp.annex.lan", origin: "https://myapp.annex.lan" };
  assert.equal(csrfViolation("POST", getter(hdrs)), null);
  assert.ok(csrfViolation("POST", getter({ ...hdrs, origin: "https://evil.example" })));
});

test("ANNEX_CSRF off turns it off", () => {
  const bad = { "sec-fetch-site": "cross-site", origin: "https://evil.example" };
  for (const off of ["off", "OFF", "false", "0", "no"]) { process.env.ANNEX_CSRF = off; assert.equal(v("POST", bad), null, off); }
  for (const on of ["on", "true", ""]) { process.env.ANNEX_CSRF = on; assert.ok(v("POST", bad), on); }
});

function run(method, headers) {
  const h = Object.fromEntries(Object.entries({ ...HOST, ...headers }).map(([k, x]) => [k.toLowerCase(), x]));
  const out = { next: false, status: 0 };
  const res = { status(s) { out.status = s; return this; }, type() { return this; }, send() { return this; } };
  csrfGuard({ method, path: "/x", get: (n) => h[n.toLowerCase()] }, res, () => { out.next = true; });
  return out;
}

test("csrfGuard answers 403 and does not call next", () => {
  const lines = []; const original = process.stdout.write;
  process.stdout.write = (c) => { lines.push(String(c)); return true; };
  let out;
  try { out = run("POST", { origin: "https://evil.example", "sec-fetch-site": "cross-site" }); } finally { process.stdout.write = original; }
  assert.deepEqual(out, { next: false, status: 403 });
  assert.match(lines.join(""), /cross-site request refused/);
});

test("csrfGuard passes same-origin and headerless posts", () => {
  assert.equal(run("POST", { origin: "https://myapp.annex.lan:8443", "sec-fetch-site": "same-origin" }).next, true);
  assert.equal(run("POST", {}).next, true);
  assert.equal(run("GET", { "sec-fetch-site": "cross-site" }).next, true);
});
