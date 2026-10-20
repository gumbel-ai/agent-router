// Exercise the production script with Node built-ins; DOM behavior is checked in a browser.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const context = vm.createContext({ fetch: () => new Promise(() => {}), setTimeout: () => {} });
vm.runInContext(fs.readFileSync(0, "utf8"), context);

const cases = [
  [{ status: "running" }, "active"],
  [{ status: "created" }, "active"],
  [{ status: "validating" }, "active"],
  [{ status: "preparing" }, "active"],
  [{ status: "retrying" }, "active"],
  [{ status: "cancelling" }, "active"],
  [{ status: "idle", inflight_tool_count: 1 }, "active"],
  [{ status: "idle", inflight_tool_count: 1, tool_visibility_partial: true }, "uncertain"],
  [{ status: "idle", needs_input: true }, "attention"],
  ...["awaiting_review", "blocked", "failed", "interrupted", "signaled"].map(status => [{ status }, "attention"]),
  ...["cleanup_failed", "shutdown_unconfirmed", "group_outlived"].map(flag => [
    { status: "closed", [flag]: true }, "attention",
  ]),
  [{ status: "unknown", needs_input: true, inflight_tool_count: 1 }, "uncertain"],
  [{ status: "unknown", shutdown_unconfirmed: true }, "attention"],
  [{}, "uncertain"],
  ...["idle", "stopped", "exited", "completed", "accepted", "integrated", "cancelled", "closed"].map(status => [
    { status }, "history",
  ]),
];
for (const [record, expected] of cases) {
  assert.equal(context.workGroup(record), expected, JSON.stringify(record));
}
assert.equal(context.workState({ status: "unknown", needs_input: true }), "unknown");
assert.equal(context.workState({ status: "idle", inflight_tool_count: 1 }), "tool_running");

// A command's owner is not its identity, even when multiple commands share that owner.
for (const managed_id of ["command-one", "command-two"]) {
  const identity = context.workIdentity({ managed_id, agent_id: "owner-agent", session_id: "owner-session" }, "Command");
  assert.equal(identity.id, managed_id);
  assert.deepEqual(JSON.parse(JSON.stringify(identity.parents)), [
    { label: "parent session", id: "owner-session" }, { label: "parent agent", id: "owner-agent" },
  ]);
}
for (const [kind, key] of [["Session", "session_id"], ["Subagent", "agent_id"], ["Router task", "task_id"]]) {
  const record = { session_id: "session", agent_id: "agent", task_id: "task", managed_id: "command" };
  assert.equal(context.workIdentity(record, kind).id, record[key]);
}

// Session lifetime never substitutes for missing execution duration, including zero and rollback.
assert.equal(context.workTiming({ started_at: 0, elapsed_ms: 10 }, "Session", 3600000), "Session age: 1h 0m");
assert.equal(context.workTiming({ started_at: 0, finished_at: 1000 }, "Session", 3600000), "Session span: 1s");
assert.equal(context.workTiming({ started_at: 0, elapsed_ms: null }, "Router task", 3600000), "Elapsed: unknown");
assert.equal(context.workTiming({ elapsed_ms: 0 }, "Command", 1000), "Elapsed: 0s");
for (const elapsed_ms of [null, undefined, -1, NaN, Infinity, "100"]) {
  assert.equal(context.workTiming({ elapsed_ms }, "Subagent", 1000), "Elapsed: unknown");
}
assert.equal(context.since(1001, 1000), "");
assert.equal(context.since(null, 1000), "");

// Preserve host-local clocks and explicit identities; never merge lookalike IDs across hosts/runtimes.
const fleet = {
  hosts: [
    { name: "laptop", ok: true, snapshot: {
      generated_at: 10000,
      sessions: [{ session_id: "same-id", source: "claude", status: "running", started_at: 9000 }],
      agents: [{ agent_id: "a", source: "claude", parent_session_id: "same-id", status: "needs_input" }],
      router_tasks: [{ task_id: "task", status: "awaiting_review" }],
      managed_processes: [{ managed_id: "command", status: "exited" }],
    } },
    { name: "worker", ok: true, snapshot: {
      generated_at: 2000,
      sessions: [{ session_id: "same-id", source: "codex", status: "unknown", started_at: 1000 }],
    } },
    { name: "offline", ok: false },
  ],
};
const original = JSON.stringify(fleet);
const items = context.collectWork(fleet);
assert.equal(items.length, 5);
assert.equal(items.find(item => item.host === "worker").group, "uncertain");
for (const item of items.filter(item => item.kind === "Session")) {
  assert.equal(context.workTiming(item.record, item.kind, item.now), "Session age: 1s");
}
assert.equal(items.find(item => item.kind === "Subagent").record.parent_session_id, "same-id");
assert.equal(JSON.stringify(fleet), original, "Rendering must not mutate fleet evidence");
console.log("Dashboard grouping, timing, attribution and unknown-state contracts passed.");
