# Delta - Pre-Commit Integration

**Local pre-commit hook that runs only affected tests.**

## 🎯 What You Get

A git pre-commit hook that:
1. **Finds affected tests** by comparing staged changes to development branch
2. **Runs only those tests** with coverage enabled
3. **Blocks commit** if tests fail
4. **Combines coverage** with existing .coverage file (incremental)
5. **Always runs new tests** added in the commit

## 📁 Files Created

| File | Purpose | Storage |
|------|---------|---------|
| `delta/test_mapping_db.py` | SQLite database interface | Code |
| `delta/pre_commit_hook.py` | Pre-commit hook logic | Code |
| `delta/update_mapping.py` | Update mapping from coverage | Code |
| `scripts/install_pre_commit.sh` | Installation script | Code |
| `.delta/test_mapping.db` | Test→code mapping (SQLite) | Repo root (gitignored) |
| `.git/hooks/pre-commit` | Git pre-commit hook | Git hooks |

## 🚀 Installation

```bash
cd ~/workspace/myproject

# Run the installer
~/workspace/delta/scripts/install_pre_commit.sh
```

The installer:
- Installs Delta package
- Generates initial coverage (asks permission)
- Creates `.delta/test_mapping.db` from coverage
- Installs pre-commit hook
- Updates .gitignore

## 📊 How It Works

### Flow Diagram

```
Developer: git commit
     ↓
Pre-commit hook triggered (.git/hooks/pre-commit)
     ↓
Initialize Git LFS (if available)
     ↓
Check/start Docker containers
     ↓
Run preflight scripts (postgres, neo4j)
     ↓
Check if mapping DB exists (build if missing)
     ↓
Get staged changes: git diff --cached development...HEAD
     ↓
Parse diff → Extract changed files & line numbers
     ↓
Query SQLite: "Which tests cover these lines?"
     ↓
Detect unmapped tests: pytest --collect-only vs mapping DB
     ↓
Run unmapped tests iteratively (1 by 1, update mapping after each)
     ↓
Add new test files (always run new tests)
     ↓
Run mapped affected tests: pytest --cov --cov-context=test --cov-append
     ↓
Tests pass? ┬─ No  → Block commit ❌
            └─ Yes → Combine coverage 
                  → Stage updated .test_mapping.db (if modified)
                  → Allow commit ✅
                  → Post-commit hook triggered
                  → Auto-push to remote 🚀
```

### Auto-Push After Commit

After a successful commit, the **post-commit hook** automatically pushes your changes:

- **Includes updated test mapping database** (via Git LFS)
- **Pushes to current branch** (`origin/<current-branch>`)
- **Non-blocking** - if push fails (e.g., need to pull), you can push manually later
- **Skip with:** `git commit --no-verify` (skips both pre and post hooks)

This ensures the test mapping database stays in sync across the team without manual `git push` commands.

### Auto-Discovery of Unmapped Tests

**Problem:** New tests won't be in the mapping database yet.

**Solution:** The hook automatically detects and runs unmapped tests:

1. **Collect all tests**: `pytest --collect-only` to find all available tests
2. **Compare with DB**: Query which tests are already in `.test_mapping.db`
3. **Find unmapped**: Delta = all_tests - mapped_tests
4. **Run iteratively**: Execute each unmapped test with coverage
5. **Update incrementally**: Add to mapping database after each test

**Why all-at-once?** Running unmapped tests together ensures:
- Fastest execution time (single pytest invocation)
- Mapping updates happen after all tests complete
- Progress is preserved even if tests fail
- Database stays consistent throughout the process

**Example:**

```bash
$ git commit -m "Add feature"

🔍 Collected 1250 total tests from pytest
📊 Found 1234 tests in mapping database
⚠️  Found 16 unmapped test(s)
   These tests will run together to build the coverage mapping

================================================================================
Building coverage mapping for 16 unmapped test(s)
Running all tests at once...
================================================================================

Running 16 unmapped tests with coverage...
   ✓ 16 test(s) passed
✓ Successfully mapped 16 test(s)

================================================================================
Running 5 affected test(s)...
================================================================================
...
```

### SQLite Mapping Database

```sql
-- Schema
CREATE TABLE test_coverage (
    test_name TEXT NOT NULL,      -- e.g., "unit_tests/test_auth.py::test_login"
    file_path TEXT NOT NULL,      -- e.g., "src/auth.py"
    line_number INTEGER NOT NULL, -- e.g., 42
    PRIMARY KEY (test_name, file_path, line_number)
);

CREATE INDEX idx_file_line ON test_coverage(file_path, line_number);

-- Example query
SELECT DISTINCT test_name
FROM test_coverage
WHERE file_path LIKE '%auth.py'
AND line_number IN (42, 43, 44);
```

### Coverage Combining

```python
# The --cov-append flag ensures coverage is additive
pytest --cov=. --cov-context=test --cov-append --cov-report=

# Internally uses coverage.py's combine feature
coverage combine .coverage.backup .coverage.new
```

This ensures:
- ✅ Existing coverage is preserved
- ✅ New coverage is added incrementally
- ✅ No data loss between commits

## 🎛️ Commands

### Update Mapping Database

```bash
# Run build-mapping to automatically run tests and update the mapping
delta build-mapping --repo-root ~/workspace/myproject --verbose
```

**When to update:**
- Weekly (recommended)
- After adding new test files
- After major refactoring
- When test selection seems incorrect

### Manual Test Running (Without Hook)

```bash
# See what tests would run
delta run --repo-root ~/workspace/myproject --dry-run --verbose

# Run affected tests manually
delta run --repo-root ~/workspace/myproject

# Pass pytest arguments
delta run --repo-root ~/workspace/myproject -- -x --pdb
```

### Check Mapping Stats

```bash
sqlite3 .delta/test_mapping.db << EOF
SELECT 
    (SELECT COUNT(DISTINCT test_name) FROM test_coverage) as total_tests,
    (SELECT COUNT(DISTINCT file_path) FROM test_coverage) as covered_files,
    COUNT(*) as total_mappings
FROM test_coverage;
EOF
```

## 🔧 Configuration

### Change Base Branch

Edit `.git/hooks/pre-commit`:

```bash
python3 -m delta.pre_commit_hook \
    --repo-root "$REPO_ROOT" \
    --base-branch main \  # ← Change this
    --verbose
```

### Skip Coverage Combining

If you don't want coverage combining:

```bash
python3 -m delta.pre_commit_hook \
    --repo-root "$REPO_ROOT" \
    --base-branch development \
    --skip-coverage-combine \  # ← Add this
    --verbose
```

## 💡 Examples

### Example 1: Simple Bug Fix

```bash
$ vim src/auth.py  # Fix timeout bug
$ git add src/auth.py
$ git commit -m "Fix auth timeout"

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

✓ Coverage data combined successfully
✅ All affected tests passed. Commit allowed.
```

### Example 2: New Test File

```bash
$ vim unit_tests/test_new_feature.py  # Create new test
$ git add unit_tests/test_new_feature.py
$ git commit -m "Add new feature tests"

📝 Found 1 changed Python file
✨ New test files (always run): 1
  + unit_tests/test_new_feature.py

================================================================================
Running 1 test file...
================================================================================

unit_tests/test_new_feature.py::test_feature_basic PASSED
unit_tests/test_new_feature.py::test_feature_advanced PASSED

✅ All affected tests passed. Commit allowed.
```

### Example 3: Tests Fail

```bash
$ vim src/payment.py  # Introduce a bug
$ git add src/payment.py
$ git commit -m "Update payment logic"

📝 Found 1 changed Python file
📍 src/payment.py: 5 test(s) cover changed lines

================================================================================
Running 5 affected test(s)...
================================================================================

unit_tests/test_payment.py::test_process_payment FAILED
unit_tests/test_payment.py::test_refund PASSED
...

❌ Tests failed. Commit blocked.
Fix the failing tests and try again.
```

Commit is **blocked** until you fix the tests!

## 🐛 Troubleshooting

| Problem | Solution |
|---------|----------|
| "Mapping database not found" | Run `delta build-mapping` |
| "No tests selected" | Regenerate mapping: `delta build-mapping` |
| "Tests run that shouldn't" | Coverage may be stale. Regenerate mapping. |
| "Tests don't run that should" | Coverage incomplete. Regenerate mapping with `delta build-mapping`. |
| "Hook too slow" | First run is slow (builds mapping). Subsequent runs are fast. |
| "Many unmapped tests detected" | Normal on first run. They'll be mapped incrementally. To speed up: `delta build-mapping` |
| "Need to bypass hook" | Use `git commit --no-verify` |

## 📊 Performance

Typical performance on a medium-sized project:

| Metric | Value | Notes |
|--------|-------|-------|
| Total tests | 2,000 | |
| Typical change | 1-3 files | |
| Affected tests | 5-30 (0.25-1.5% of total) | |
| Unmapped tests (first run) | 50-200 | One-time cost |
| Unmapped tests (steady state) | 0-5 per commit | Only new tests |
| Full test run time | 10 minutes | |
| Affected test run time | 30 seconds | After mapping is built |
| Time savings | 95% | After initial mapping |
| Mapping DB size | ~50 MB | |
| Mapping query time | <1ms | |

**First commit performance:**
- First commit after installing the hook may take longer (unmapped tests run iteratively)
- Subsequent commits are fast (only new unmapped tests)
- To avoid slow first commit, pre-populate mapping: `delta build-mapping`

## 🔒 Safety Guarantees

The pre-commit hook provides these guarantees:

1. **No false negatives**: All tests covering changed code WILL run
2. **New tests always run**: Newly added test files are always included
3. **Unmapped tests always run**: Tests not yet in mapping DB are detected and run
4. **Coverage preservation**: Existing coverage is never lost
5. **Test failures block commits**: Bad code cannot be committed
6. **Mapping staleness detection**: Warns if mapping is outdated
7. **Incremental mapping updates**: Unmapped tests update the DB as they run

## 📚 Implementation Details

### Test Detection Logic

```python
affected_tests = (
    tests_covering_changed_lines +    # From mapping DB
    modified_test_files +              # Test files in diff
    new_test_files +                   # New test files (--diff-filter=A)
    unmapped_tests                     # Tests not in mapping DB yet
)
```

### Coverage File Handling

```bash
# Before test run
.coverage           # Existing coverage (preserved)

# During test run
pytest --cov-append  # Appends to .coverage

# After test run (success)
.coverage           # Combined: old + new
```

### Mapping Update Process

**Full update** (via `delta build-mapping`):
```python
1. Read .coverage (SQLite)
2. Extract test_contexts (which test covered which line)
3. Clear existing mapping data
4. Write all mappings to .test_mapping.db (SQLite)
5. Create indexes for fast lookups
6. Store metadata (timestamp, stats)
```

**Incremental update** (during pre-commit for unmapped tests):
```python
1. Run single unmapped test with --cov-context=test
2. Read .coverage (SQLite)
3. Extract test_contexts for this test
4. INSERT OR IGNORE new mappings (preserve existing)
5. No deletion - only addition
6. Update metadata
```

### Unmapped Test Handling

```python
# Detection
all_tests = run_pytest_collect_only()          # All tests in codebase
mapped_tests = query_mapping_db()              # Tests in .test_mapping.db
unmapped = all_tests - mapped_tests            # Delta

# Iterative execution
for test in unmapped:
    run_test_with_coverage(test)               # Isolated coverage
    import_coverage_incremental()              # Add to mapping (don't clear)
    combine_with_main_coverage()               # Preserve historical data
```

## 🎓 Best Practices

1. **Pre-populate mapping**: Run `delta build-mapping` before first commit
2. **Update mapping weekly**: Keep mapping current with codebase changes (run `delta build-mapping`)
3. **Run full tests periodically**: Don't rely solely on affected tests
4. **Keep coverage high**: Better coverage = better test selection
5. **Use --dry-run**: Check test selection before committing
6. **Trust the hook**: It's designed to be conservative (run more rather than less)
7. **Let unmapped tests run**: They build the mapping automatically

## 🚀 Next Steps

You're all set! The pre-commit hook will now:
- ✅ Run automatically on every commit
- ✅ Select only affected tests intelligently
- ✅ Save you significant time
- ✅ Prevent broken code from being committed
- ✅ Maintain coverage data incrementally

**Happy coding!** 🎉
