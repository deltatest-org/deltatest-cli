# Quick Start - Pre-Commit Hook

Get started with Pintest in **5 minutes**.

## Step 1: Install (1 minute)

```bash
cd ~/workspace/myproject

# Run the installer
~/workspace/all/pintest/scripts/install_pre_commit.sh
```

The installer will:
- ✅ Install the pintest package
- ✅ Generate initial coverage (if needed)
- ✅ Create test mapping database
- ✅ Install pre-commit hook
- ✅ Update .gitignore

## Step 2: Make a Change (1 minute)

```bash
# Edit a file
vim src/my_module.py

# Stage your changes
git add src/my_module.py
```

## Step 3: Commit (1-3 minutes)

```bash
git commit -m "Fix bug in my_module"

# The pre-commit hook runs automatically:
# - Finds tests covering your changes
# - Runs only those tests (much faster!)
# - Blocks commit if tests fail
# - Combines coverage on success
```

## That's It!

From now on, every commit will:
1. **Find** tests affected by your code changes
2. **Run** only those tests (not the entire suite)
3. **Block** the commit if tests fail
4. **Combine** coverage with existing data

## Example Output

```bash
$ git commit -m "Fix authentication timeout"

📝 Found 1 changed Python file
📊 Mapping DB: 1234 tests, 567 files, 45678 mappings
📍 src/auth.py: 3 test(s) cover changed lines
    - unit_tests/test_auth.py::test_login_timeout
    - unit_tests/test_auth.py::test_login_retry
    - integration_tests/test_full_auth_flow.py::test_complete_flow

================================================================================
Running 3 affected test(s)...
================================================================================

unit_tests/test_auth.py::test_login_timeout PASSED
unit_tests/test_auth.py::test_login_retry PASSED
integration_tests/test_full_auth_flow.py::test_complete_flow PASSED

✅ All affected tests passed. Commit allowed.
[feature/fix-auth 1a2b3c4] Fix authentication timeout
 1 file changed, 5 insertions(+), 2 deletions(-)
```

## What Just Happened?

1. **Git hook triggered** when you ran `git commit`
2. **Staged changes analyzed**: `git diff --cached development...HEAD`
3. **Mapping queried**: "Which tests cover src/auth.py lines 42-46?"
4. **Tests run**: Only 3 tests (not all 1234!)
5. **Coverage combined**: New coverage merged with existing data

## ⚡ Time Savings

| Scenario | Before (all tests) | After (affected) | Savings |
|----------|-------------------|------------------|---------|
| Small change (1-2 files) | 10 minutes | 30 seconds | 95% |
| Medium change (5-10 files) | 10 minutes | 2 minutes | 80% |
| Large refactor (20+ files) | 10 minutes | 5 minutes | 50% |

## 🛠️ Maintenance

### Update Mapping (Weekly Recommended)

```bash
cd ~/workspace/myproject

# Build/update the test mapping database
delta build-mapping --verbose
```

**When to update:**
- After adding new test files
- After significant refactoring  
- Weekly (for active projects)
- When tests aren't being selected correctly

### Check Hook Status

```bash
# See what tests would run for current changes
pintest run --repo-root ~/workspace/myproject --dry-run --verbose

# Check mapping database stats
pintest update-mapping --repo-root ~/workspace/myproject --verbose
```

## 🚫 Bypassing the Hook

For urgent commits or when the hook is broken:

```bash
# Skip the hook
git commit --no-verify -m "Urgent hotfix"

# Or temporarily disable
mv .git/hooks/pre-commit .git/hooks/pre-commit.disabled
git commit -m "My commit"
mv .git/hooks/pre-commit.disabled .git/hooks/pre-commit
```

## 🐛 Troubleshooting

**"No tests selected"**

```bash
# Regenerate mapping
delta build-mapping --verbose
```

**"Mapping database not found"**

```bash
delta build-mapping --repo-root ~/workspace/myproject --verbose
```

**"Hook takes too long"**

The first run may be slow (generating initial coverage). Subsequent runs are much faster because:
- Only affected tests run (typically 2-5% of total)
- Mapping database enables fast lookups
- Coverage combining is incremental

**"Tests that should run aren't selected"**

Coverage may be stale. Regenerate:

```bash
delta build-mapping
```

## 📚 Advanced Usage

### Change Base Branch

Edit `.git/hooks/pre-commit`:

```bash
python3 -m pintest.pre_commit_hook \
    --repo-root "$REPO_ROOT" \
    --base-branch main \  # Change this
    --verbose
```

### Manual Test Running

```bash
# See what would run
pintest run --repo-root ~/workspace/myproject --dry-run

# Run manually (not via hook)
pintest run --repo-root ~/workspace/myproject

# Pass pytest arguments
pintest run --repo-root ~/workspace/myproject -- -x --pdb
```

### View Mapping Stats

```bash
# Check database size and stats
ls -lh .test_mapping.db
sqlite3 .test_mapping.db "SELECT COUNT(*) FROM test_coverage"
sqlite3 .test_mapping.db "SELECT COUNT(DISTINCT test_name) FROM test_coverage"
```

## 💡 Tips

1. **Keep mapping fresh**: Update weekly or after major changes
2. **Use --dry-run**: Check what tests will run before committing
3. **Commit often**: Smaller changes = fewer affected tests = faster
4. **Coverage matters**: Better coverage = better test selection
5. **Trust the hook**: It blocks bad commits, embrace it!

## 🔗 Related Files

- `.test_mapping.db` - Test mapping database (gitignored)
- `.coverage` - pytest coverage data (gitignored)
- `.git/hooks/pre-commit` - Pre-commit hook script

## 🎯 Next Steps

You're all set! The hook will now:
- ✅ Run automatically on every commit
- ✅ Only run affected tests
- ✅ Save you tons of time
- ✅ Keep your codebase healthy

**Happy coding!** 🚀
- **Set --min-tests**: Ensure at least some tests run (fallback to full suite if too few)
- **Combine with pytest args**: Use `--` to pass additional pytest arguments

## Troubleshooting

### "Coverage file not found" or "Mapping database not found"
Run: `delta build-mapping --verbose` in your repo first

### "No coverage data found for changed files"
- Coverage DB might be stale - regenerate it
- File paths might not match - check with `--verbose` flag
- New files won't have coverage (expected)

### "No tests to run"
- No tests cover your changes (write tests!)
- Coverage DB doesn't have test context data
- Try running with `--verbose` to see detailed matching

## Advanced Usage

### Custom Coverage File Location

```bash
pintest \
    --repo-root ~/workspace/myproject \
    --coverage-file /custom/path/.coverage
```

### Integration with Pre-commit Hook

Create `.git/hooks/pre-push`:
```bash
#!/bin/bash
pintest --repo-root . --dry-run || exit 0
```

### Multiple Repositories

```bash
# Project A
pintest --repo-root ~/projects/project-a

# Project B  
pintest --repo-root ~/projects/project-b
```
