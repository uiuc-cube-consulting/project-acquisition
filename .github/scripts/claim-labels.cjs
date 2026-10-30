// Assignees are the source of truth. Never infer a claim from comment text.
// Run only trusted default-branch code, not a contributor's PR checkout.
const PREFIX = "claimed-by:";

function expectedLabels(assignees) {
  return [...new Set(assignees.map(({ login }) => {
    if (!/^[a-z0-9][a-z0-9-]{0,38}$/i.test(login)) throw new Error("Invalid GitHub login");
    return `${PREFIX}${login.toLowerCase()}`;
  }))].sort();
}

async function syncIssue({ github, context, issueNumber }) {
  const repo = context.repo;
  // Read current state, not the event snapshot (assign/unassign jobs can queue).
  const { data: issue } = await github.rest.issues.get({ ...repo, issue_number: issueNumber });
  if (issue.pull_request) return;
  const expected = expectedLabels(issue.assignees || []);
  const existing = (issue.labels || []).map((label) => typeof label === "string" ? label : label.name);
  for (const name of expected.filter((name) => !existing.some((label) => label.toLowerCase() === name))) {
    try {
      await github.rest.issues.createLabel({ ...repo, name, color: "5a1ca9", description: `Claimed by @${name.slice(PREFIX.length)}; synced from issue assignees.` });
    } catch (error) {
      // Another issue's job may already have created this repository label.
      if (error.status !== 422) throw error;
      await github.rest.issues.getLabel({ ...repo, name });
    }
    await github.rest.issues.addLabels({ ...repo, issue_number: issueNumber, labels: [name] });
  }
  for (const name of existing.filter((name) => name.toLowerCase().startsWith(PREFIX) && !expected.includes(name.toLowerCase()))) {
    try { await github.rest.issues.removeLabel({ ...repo, issue_number: issueNumber, name }); }
    catch (error) { if (error.status !== 404) throw error; }
  }
}

module.exports = { expectedLabels, syncIssue };
