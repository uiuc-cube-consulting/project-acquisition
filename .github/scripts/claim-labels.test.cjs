const { test } = require("node:test");
const assert = require("node:assert/strict");
const { expectedLabels, syncIssue } = require("./claim-labels.cjs");

function fixture(issue) {
  const calls = [];
  const issues = {};
  for (const name of ["get", "createLabel", "getLabel", "addLabels", "removeLabel"]) {
    issues[name] = async (args) => { calls.push([name, args]); return name === "get" ? { data: issue } : {}; };
  }
  return { github: { rest: { issues } }, context: { repo: { owner: "example-org", repo: "example-repo" } }, issueNumber: 12, calls };
}
test("claim labels use normalized, deduplicated assignee logins", () => {
  assert.deepEqual(expectedLabels([{ login: "Example-Dev" }, { login: "example-dev" }, { login: "another" }]), ["claimed-by:another", "claimed-by:example-dev"]);
  assert.throws(() => expectedLabels([{ login: "bad login" }]));
});
test("adds claimed-by labels without replacing area/size labels or posting comments", async () => {
  const f = fixture({ assignees: [{ login: "example-dev" }], labels: [{ name: "bug" }, { name: "area: metrics" }] });
  await syncIssue(f);
  assert.deepEqual(f.calls.map(([n]) => n), ["get", "createLabel", "addLabels"]);
  assert.deepEqual(f.calls[2][1].labels, ["claimed-by:example-dev"]);
});
test("unassignment removes only stale claim labels", async () => {
  const f = fixture({ assignees: [], labels: [{ name: "claimed-by:example-dev" }, { name: "bug" }] });
  await syncIssue(f);
  assert.equal(f.calls.length, 2);
  assert.equal(f.calls[1][1].name, "claimed-by:example-dev");
});
test("reassignment adds the new label and removes the old one", async () => {
  const f = fixture({ assignees: [{ login: "new-dev" }], labels: ["claimed-by:old-dev", "size: S"] });
  await syncIssue(f);
  assert.deepEqual(f.calls.map(([n]) => n), ["get", "createLabel", "addLabels", "removeLabel"]);
});
test("multiple assignees get one label each; synchronized issues are no-ops", async () => {
  const f = fixture({ assignees: [{ login: "one" }, { login: "two" }], labels: ["claimed-by:one", "claimed-by:two", "bug"] });
  await syncIssue(f);
  assert.equal(f.calls.length, 1);
});
test("a concurrent label creation is safe", async () => {
  const f = fixture({ assignees: [{ login: "one" }], labels: [] });
  f.github.rest.issues.createLabel = async () => { throw { status: 422 }; };
  await syncIssue(f);
  assert.deepEqual(f.calls.map(([n]) => n), ["get", "getLabel", "addLabels"]);
});
test("an already removed label is safe, but permission failures propagate", async () => {
  const f = fixture({ assignees: [], labels: ["claimed-by:one"] });
  f.github.rest.issues.removeLabel = async () => { throw { status: 404 }; };
  await syncIssue(f);
  f.github.rest.issues.removeLabel = async () => { throw { status: 403 }; };
  await assert.rejects(syncIssue(f));
});
test("pull requests are ignored", async () => {
  const f = fixture({ pull_request: {}, assignees: [{ login: "one" }], labels: [] });
  await syncIssue(f);
  assert.equal(f.calls.length, 1);
});
