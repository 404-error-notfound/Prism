# Issue tracker: GitHub

Issues and PRDs for this repo live as GitHub issues on the `404-error-notfound/Prism` repository. Use the [`gh`](https://cli.github.com) CLI for all operations.

## Conventions

- **Create an issue**: `gh issue create --title "..." --body "..."`. Use a heredoc for multi-line bodies. Pass `--body-file -` to read from stdin.
- **Read an issue**: `gh issue view <number> --comments`. Use `--json` for machine-readable output.
- **List issues**: `gh issue list --json ...` with appropriate `--label` filters.
- **Comment on an issue**: `gh issue comment <number> --body "..."`.
- **Apply / remove labels**: `gh issue edit <number> --add-label "..."` / `--remove-label "..."`. Multiple labels can be comma-separated.
- **Close**: `gh issue close <number> --comment "..."` posts a closing comment and closes in one step.
- **Pull requests**: `gh pr create`, `gh pr view`, `gh pr comment`, etc.

`gh` infers the repo from `git remote -v` when run inside a clone.

## When a skill says "publish to the issue tracker"

Create a GitHub issue.

## When a skill says "fetch the relevant ticket"

Run `gh issue view <number> --comments`.
