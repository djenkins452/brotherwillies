# Close out the current coding session

model: sonnet

## Purpose

Review the current coding session and ensure all documentation, help systems, and tracking are up to date before ending work.

## Session Review Checklist

Perform each of the following checks and updates as needed:

### 1. Changelog Review

Read `docs/changelog.md` and verify:
- All changes made this session are documented
- Each entry has: date, summary, files changed, and reason
- Migration names are included if any migrations were created

If anything is missing, add it now.

### 2. What's New (Release Notes)

Check if any user-facing features were added or changed this session.

If yes, update `templates/accounts/whats_new.html`:
- Add a new release section at the top (below the TOC)
- Include: feature name, description, date, and how to use it
- Update the TOC at the top of the page with a new entry
- Follow the existing HTML card structure and styling conventions

### 3. Help System Updates

If any new pages, features, or navigation destinations were added:
- Update `templates/includes/help_modal.html` with new `help_key` entries
- Add entries for new destinations with appropriate name, description, and guidance
- Follow the existing pattern of context-aware help keyed by `help_key`

### 4. User Guide Updates

If any new features or pages were added that users should know about:
- Update `templates/accounts/user_guide.html`
- Add coverage for new features in the appropriate section
- Keep the TOC in sync

### 5. CLAUDE.md Updates

Review if any new instructions should be added to CLAUDE.md:
- New project patterns or conventions established
- New URL routes or API endpoints
- New management commands
- New environment variables
- Changes to deployment or testing procedures

Ask the user before making significant changes to CLAUDE.md.

### 6. Git: Commit, Pull, Push

**IMPORTANT: Multiple sessions may be working in parallel on the same branch.** Follow this exact order to avoid conflicts:

1. **Commit this session's work** — stage and commit only the files changed in this session
2. **Pull with rebase** — `git pull --rebase origin main` to pick up any commits pushed by other sessions
3. **If rebase conflicts occur:**
   - Show the user the conflicting files and ask how to resolve
   - Do NOT force-push or drop other sessions' commits
4. **Push** — `GIT_SSH_COMMAND="ssh -p 443" git push git@ssh.github.com:djenkins452/brotherwillies.git main`
5. **Verify** — `git status` and `git log --oneline -5` to confirm clean state

If there are uncommitted files that do NOT belong to this session's work (e.g., PLAN.md from another task, files you didn't touch), leave them alone — another session owns them.

## Output

After completing all checks, provide a **Session Summary** with:

1. **Changes Made This Session:**
   - List of features, fixes, or updates completed

2. **Documentation Updated:**
   - Changelog: Yes/No (entries added)
   - What's New: Yes/No/N/A
   - Help System: Yes/No/N/A
   - User Guide: Yes/No/N/A
   - CLAUDE.md: Yes/No

3. **Git Status:**
   - Branch: (current branch)
   - Uncommitted changes: Yes/No
   - Pushed to remote: Yes/No

4. **Ready to Close:** Yes/No

## Authority

Full authority to read files, run tests, make commits, and push.
Ask user before making significant changes to CLAUDE.md.
