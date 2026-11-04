# Plan: require CI to pass before code reaches `main`

Status: not started. Written 2026-11-04 for the operator to apply.
This is a repository setting, not a code change. **The operator applies it.** It
takes effect immediately on a public repo, so a session must not run the `POST`
below without their explicit go-ahead.

## Goal

Make a failing CI run block a change from reaching `main`.

## Current state

Checked on 2026-11-04 for `darylalim/event-planner-agent`:

- Classic branch protection on `main`: none.
- Repository rulesets, and rules applying to `main`: none.
- Merged pull requests and merge commits: none in the recent history checked.
  Work has gone straight to `main`, so CI reports problems only afterwards.

## The change this forces

A required check also applies to direct pushes: a commit pushed to `main` has no
passing checks yet, so the push is rejected. **From now on, every change goes
through a branch and a pull request**, including small docs edits and release
version bumps.

## Decisions

- **Use a ruleset**, not classic branch protection. It is the current mechanism,
  and it can be disabled without being deleted.
- **No required reviews.** A sole maintainer can't approve their own pull request,
  so that rule would block every merge.
- **No bypass actors.** The admin can still disable the ruleset in an emergency,
  which is deliberate and visible, unlike a routine bypass.
- **Not strict.** A branch doesn't have to be up to date with `main` before merging.
  Revisit this if more contributors join.
- **Block force pushes and deletion of `main`.**

## Required checks

Use these six names exactly, including the `·` characters. They were read from
the check runs on `main`.

```text
lint · format · types
tests (py3.11)
tests (py3.12)
tests (py3.13)
tests (py3.14)
deployed graph imports
```

**Do not require `tag · release`.** It appears on `main` commits, but `release.yml`
runs from `workflow_run` after CI on `main`. It never reports on a pull request, so
requiring it would block every merge indefinitely.

## Steps

1. Confirm the checks' app id. The JSON below assumes GitHub Actions is `15368`:

   ```bash
   gh api repos/darylalim/event-planner-agent/commits/main/check-runs \
     --jq '.check_runs[0].app.id'
   ```

2. Save this as `ruleset.json`, outside the repo:

   ```json
   {
     "name": "main requires CI",
     "target": "branch",
     "enforcement": "active",
     "conditions": { "ref_name": { "include": ["~DEFAULT_BRANCH"], "exclude": [] } },
     "rules": [
       { "type": "deletion" },
       { "type": "non_fast_forward" },
       {
         "type": "required_status_checks",
         "parameters": {
           "strict_required_status_checks_policy": false,
           "required_status_checks": [
             { "context": "lint · format · types", "integration_id": 15368 },
             { "context": "tests (py3.11)", "integration_id": 15368 },
             { "context": "tests (py3.12)", "integration_id": 15368 },
             { "context": "tests (py3.13)", "integration_id": 15368 },
             { "context": "tests (py3.14)", "integration_id": 15368 },
             { "context": "deployed graph imports", "integration_id": 15368 }
           ]
         }
       }
     ]
   }
   ```

3. Commit anything meant for `main` first, including `docs/plans/`, or plan to send
   it through a pull request.

4. Apply it:

   ```bash
   gh api repos/darylalim/event-planner-agent/rulesets --method POST --input ruleset.json
   ```

5. Record the returned `id` for rollback.

## Verification

1. `gh api repos/darylalim/event-planner-agent/rules/branches/main` lists all three
   rule types.
2. Open a throwaway pull request with a harmless change. Confirm all six checks
   report and that merging is blocked until they finish.
3. Confirm that merging works once they pass. This proves no check name is wrong,
   since a misnamed check waits forever.
4. Confirm a direct push to `main` is rejected.
5. Confirm `release.yml` still runs after a merge. It is triggered by CI completing
   on `main`, and creating a tag is not covered by a rule on the `main` branch.

## Rollback

```bash
gh api repos/darylalim/event-planner-agent/rulesets/<id> --method PUT \
  -f enforcement=disabled
```

## Keeping it working

Required checks are matched by name, so these changes need a ruleset update in the
same pull request window:

- Renaming a job in `ci.yml`.
- Changing the Python matrix. Dropping 3.11 from the matrix, for example, leaves
  `tests (py3.11)` waiting forever.

`ci.yml`'s `pull_request` trigger has no path filter. Keep it that way: a path filter
would stop checks reporting on some pull requests, and those could never merge.

Consider adding a short note on this to `CLAUDE.md` and `README.md` once applied.

## Order

Apply this before `dependency-upgrade.md`, so the upgrade lands through the full
Python matrix and `deploy-shape`, which only CI runs.
