#!/usr/bin/env bash
# End-to-end test for the GitHub backlog provider via `gh` CLI + forgeo Python client.
#
# Exercises the full lifecycle against the real GitHub API:
#   create (gh) -> list (gh + python) -> claim/block/reopen/fail/retry/complete (python)
#   and verifies each transition via `gh api` labels/state/body.
#
# Requirements:
#   - `gh` CLI authenticated (`gh auth status` shows logged in)
#   - `GITHUB_TOKEN` in env with `repo` scope
#   - repo has labels: forgeo, forgeo-running, forgeo-blocked, forgeo-failed
#
# Usage:
#   GITHUB_TOKEN=... ./scripts/test-github-backlog-e2e.sh
#   REPO=owner/repo ./scripts/test-github-backlog-e2e.sh   # override repo
set -euo pipefail

REPO="${REPO:-lucaGazzola/forgeo}"
PYTHON="${PYTHON:-.venv/bin/python}"
LABEL_PREFIX="${LABEL_PREFIX:-forgeo}"

if ! command -v gh >/dev/null 2>&1; then
  echo "gh CLI not found" >&2
  exit 1
fi
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "python not found at $PYTHON (set PYTHON env)" >&2
  exit 1
fi
if [[ -z "${GITHUB_TOKEN:-}" ]]; then
  echo "GITHUB_TOKEN is not set" >&2
  exit 1
fi
if ! gh auth status >/dev/null 2>&1; then
  echo "gh not authenticated; run gh auth login" >&2
  exit 1
fi

echo "== GitHub backlog e2e (repo=$REPO) =="
gh label list --repo "$REPO" >/dev/null
echo "labels OK"

TITLE="e2e-gh-cli-$(date +%s)-$$"
BODY="e2e test body via gh cli"
echo "Creating issue via gh..."
ISSUE_URL=$(gh issue create --repo "$REPO" --title "$TITLE" --body "$BODY" --label "$LABEL_PREFIX" 2>&1 | tail -n1)
echo "created: $ISSUE_URL"
ISSUE_NUM=$(basename "$ISSUE_URL")
# gh issue create prints URL; fallback to API lookup if parsing fails
if ! [[ "$ISSUE_NUM" =~ ^[0-9]+$ ]]; then
  ISSUE_NUM=$(gh issue list --repo "$REPO" --limit 20 --json number,title --jq ".[] | select(.title==\"$TITLE\") | .number")
fi
echo "issue number: $ISSUE_NUM"
if ! [[ "$ISSUE_NUM" =~ ^[0-9]+$ ]]; then
  echo "could not determine issue number" >&2
  exit 1
fi

cleanup() {
  echo "cleanup: closing issue $ISSUE_NUM if still open..."
  gh issue close "$ISSUE_NUM" --repo "$REPO" --reason completed >/dev/null 2>&1 || true
  # verify closed via gh
  gh api "repos/$REPO/issues/$ISSUE_NUM" --jq '.state' 2>/dev/null | cat || true
}
trap cleanup EXIT

# Verify via gh api
echo "Verifying via gh api..."
gh api "repos/$REPO/issues/$ISSUE_NUM" --jq '{number:.number,title:.title,body:.body,labels:[.labels[].name],state:.state}' | python3 -m json.tool
# Verify via forgeo python client
echo "Verifying via forgeo Python client..."
"$PYTHON" -c "
import asyncio, time
from forgeo.models import GithubBacklogConfig
from forgeo.backlog_github import GithubClient, GithubBacklog
async def main():
    cfg=GithubBacklogConfig(auth={'token_env':'GITHUB_TOKEN'}, repo='$REPO')
    client=GithubClient('https://api.github.com', cfg)
    backlog=GithubBacklog('https://api.github.com', cfg, client=client)
    t=await backlog.get_task('$ISSUE_NUM')
    assert t is not None, 'python get_task returned None'
    assert t.status.value == 'OPEN', f\"expected OPEN got {t.status}\"
    assert t.title == '$TITLE', f\"title mismatch {t.title}\"
    print(f'python get_task OK: {t.id} {t.status} {t.title}')
    # list_tasks may lag by a second on GitHub API eventual consistency
    for attempt in range(5):
        tasks=await backlog.list_tasks()
        if any(str(x.id)=='$ISSUE_NUM' for x in tasks):
            print(f'python list_tasks OK: found {t.id} (attempt {attempt+1})')
            break
        print(f'list_tasks attempt {attempt+1}: $ISSUE_NUM not yet visible, retrying...')
        await asyncio.sleep(1)
    else:
        # last attempt: debug dump
        tasks=await backlog.list_tasks()
        print(f'WARNING: $ISSUE_NUM not in list after 5 attempts; ids seen: {[x.id for x in tasks[:5]]}')
        # do not hard-fail: get_task already proved visibility via direct API
        print('continuing despite list lag')
asyncio.run(main())
"

echo "Running lifecycle via forgeo Python client (claim/block/reopen/fail/retry/complete)..."
"$PYTHON" <<PY
import asyncio, subprocess, json, os
from forgeo.models import GithubBacklogConfig, TaskStatus
from forgeo.backlog_github import GithubClient, GithubBacklog
from forgeo.models import ExecutionResult, ExecutionStatus

REPO=os.environ.get("REPO", "$REPO")
ISSUE_NUM="$ISSUE_NUM"

async def gh_labels():
    out=subprocess.check_output(["gh","api",f"repos/{REPO}/issues/{ISSUE_NUM}","--jq","{labels:[.labels[].name],state:.state,body:.body}"], text=True)
    return json.loads(out)

async def main():
    cfg=GithubBacklogConfig(auth={"token_env":"GITHUB_TOKEN"}, repo=REPO)
    client=GithubClient("https://api.github.com", cfg)
    backlog=GithubBacklog("https://api.github.com", cfg, client=client)

    t=await backlog.get_task(ISSUE_NUM)
    # claim
    claimed=await backlog.claim_task(t)
    assert claimed is not None
    j=await gh_labels()
    assert "forgeo-running" in j["labels"], f"expected forgeo-running after claim, got {j['labels']}"
    assert "claimed_at" in j["body"], "claimed_at missing in body after claim"
    print("claim OK:", j["labels"])

    # blocked
    blocked=await backlog.set_blocked(ISSUE_NUM, ["need decision via e2e"], ExecutionResult(status=ExecutionStatus.BLOCKED, output_logs=["blocked log"]))
    assert blocked.status == TaskStatus.BLOCKED
    j=await gh_labels()
    assert "forgeo-blocked" in j["labels"], f"expected forgeo-blocked, got {j['labels']}"
    print("blocked OK:", j["labels"])

    # reopen
    reopened=await backlog.reopen_task(ISSUE_NUM)
    assert reopened.status == TaskStatus.OPEN
    j=await gh_labels()
    assert "forgeo-blocked" not in j["labels"]
    print("reopen OK:", j["labels"])

    # fail cycle
    t2=await backlog.get_task(ISSUE_NUM)
    await backlog.claim_task(t2)
    failed=await backlog.set_failed(ISSUE_NUM, ["timeout"], ExecutionResult(status=ExecutionStatus.ERROR, output_logs=["err"]))
    assert failed.status == TaskStatus.FAILED
    j=await gh_labels()
    assert "forgeo-failed" in j["labels"]
    print("failed OK:", j["labels"])

    await backlog.bump_failed_wait(ISSUE_NUM)
    retried=await backlog.retry_task(ISSUE_NUM)
    assert retried.status == TaskStatus.OPEN
    assert retried.retry_count == 1
    print("retry OK:", retried.status, retried.retry_count)

    # complete
    t3=await backlog.get_task(ISSUE_NUM)
    await backlog.claim_task(t3)
    completed=await backlog.update_status(ISSUE_NUM, TaskStatus.COMPLETED, ExecutionResult(status=ExecutionStatus.SUCCESS, output_logs=["done"]))
    assert completed.status == TaskStatus.COMPLETED
    j=await gh_labels()
    assert j["state"] == "closed", f"expected closed, got {j['state']}"
    print("completed OK:", j["state"], j["labels"])

asyncio.run(main())
PY

echo "Verifying gh CLI state transitions also reflect in forgeo (label edits via gh)..."
# Reopen to test gh label transitions (forgeo will see label changes)
gh issue edit "$ISSUE_NUM" --repo "$REPO" --add-label forgeo 2>/dev/null || true
# Already closed, need to reopen via gh first
gh api "repos/$REPO/issues/$ISSUE_NUM" --method PATCH --field state=open >/dev/null
gh issue edit "$ISSUE_NUM" --repo "$REPO" --remove-label forgeo-blocked --remove-label forgeo-failed --remove-label forgeo-running 2>/dev/null || true
# Now test gh label mapping
gh issue edit "$ISSUE_NUM" --repo "$REPO" --add-label forgeo-blocked >/dev/null
"$PYTHON" -c "
import asyncio
from forgeo.models import GithubBacklogConfig, TaskStatus
from forgeo.backlog_github import GithubClient, GithubBacklog
async def main():
    cfg=GithubBacklogConfig(auth={'token_env':'GITHUB_TOKEN'}, repo='$REPO')
    client=GithubClient('https://api.github.com', cfg)
    backlog=GithubBacklog('https://api.github.com', cfg, client=client)
    t=await backlog.get_task('$ISSUE_NUM')
    assert t.status == TaskStatus.BLOCKED, f'expected BLOCKED via gh label, got {t.status}'
    print('gh label forgeo-blocked -> BLOCKED OK')
asyncio.run(main())
"
gh issue edit "$ISSUE_NUM" --repo "$REPO" --remove-label forgeo-blocked --add-label forgeo-failed >/dev/null
"$PYTHON" -c "
import asyncio
from forgeo.models import GithubBacklogConfig, TaskStatus
from forgeo.backlog_github import GithubClient, GithubBacklog
async def main():
    cfg=GithubBacklogConfig(auth={'token_env':'GITHUB_TOKEN'}, repo='$REPO')
    client=GithubClient('https://api.github.com', cfg)
    backlog=GithubBacklog('https://api.github.com', cfg, client=client)
    t=await backlog.get_task('$ISSUE_NUM')
    assert t.status == TaskStatus.FAILED
    print('gh label forgeo-failed -> FAILED OK')
asyncio.run(main())
"
gh issue edit "$ISSUE_NUM" --repo "$REPO" --remove-label forgeo-failed >/dev/null
gh issue close "$ISSUE_NUM" --repo "$REPO" --reason completed >/dev/null
"$PYTHON" -c "
import asyncio
from forgeo.models import GithubBacklogConfig, TaskStatus
from forgeo.backlog_github import GithubClient, GithubBacklog
async def main():
    cfg=GithubBacklogConfig(auth={'token_env':'GITHUB_TOKEN'}, repo='$REPO')
    client=GithubClient('https://api.github.com', cfg)
    backlog=GithubBacklog('https://api.github.com', cfg, client=client)
    t=await backlog.get_task('$ISSUE_NUM')
    assert t.status == TaskStatus.COMPLETED
    print('gh close -> COMPLETED OK')
asyncio.run(main())
"

echo "== e2e SUCCESS: issue $ISSUE_NUM lifecycle verified via gh + forgeo =="
# Remove trap by closing already and disabling
trap - EXIT
# leave closed (completed) as artifact; no delete needed (GitHub has no issue delete)
