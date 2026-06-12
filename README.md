# Delta

**Run only the tests affected by your code changes** - automatically!

Delta integrates with your git workflow as a pre-commit hook, running only tests that cover the code you've changed. This dramatically speeds up your development cycle while ensuring quality.

## 🎯 Key Features

- **🚀 Fast**: Run only affected tests, not the entire suite
- **🔒 Safe**: Blocks commits if affected tests fail
- **📊 Intelligent**: Uses SQLite-based test-to-code mapping for instant lookups
- **🔄 Incremental Coverage**: Combines coverage from test runs
- **✨ New Test Detection**: Always runs newly added tests
- **🧠 Auto-Discovery**: Detects unmapped tests and builds mapping automatically
- **🎣 Pre-commit Hook**: Automatic integration with git workflow
- **🔧 Status Check**: Instantly inspect database mapping stats (both local and cloud via [deltatest.dev](https://deltatest.dev))
- **☁️ Delta Cloud Sync**: Share test mappings across CI/CD and teams automatically (Powered by [deltatest.dev](https://deltatest.dev))

## ⚡ Quick Start (Pre-Commit Hook)

```bash
cd ~/workspace/myproject

# Install the pre-commit hook
delta install

# That's it! Now every commit will:
# 1. Find tests affected by your changes
# 2. Run only those tests
# 3. Block commit if tests fail
# 4. Combine coverage with existing data
```

## 📋 How It Works

### Pre-Commit Flow

```
Developer commits changes
         ↓
Pre-commit hook triggered
         ↓
Compare staged changes vs development branch
         ↓
Query SQLite mapping: "Which tests cover these lines?"
         ↓
Detect unmapped tests (dry-run pytest --collect-only)
         ↓
Run unmapped tests all-at-once (build mapping)
         ↓
Run mapped affected tests with coverage
         ↓
Tests pass? → Combine coverage → Allow commit ✅
Tests fail? → Block commit ❌
```

### Auto-Discovery of Unmapped Tests

The pre-commit hook automatically detects tests that exist in your codebase but aren't in the mapping database yet:

1. **Collect all tests**: Runs `pytest --collect-only` to find all available tests
2. **Compare with mapping**: Queries `.delta/test_mapping.db` to see which tests are already mapped
3. **Find delta**: Identifies tests that exist but have never been run with coverage
4. **Run all-at-once**: Executes all unmapped tests together with `--cov-context=test` (fastest)
5. **Update mapping**: After completion, updates the mapping database with new coverage

**Why this matters:**
- New tests you write are automatically added to the mapping
- Tests added by teammates get mapped when you first commit
- No need to manually regenerate the entire mapping
- Mapping database grows organically over time
- All tests run in a single pytest invocation (fastest possible)

**Example:**

```bash
$ git commit -m "Fix bug in auth.py"

📝 Found 1 changed Python file
📊 Mapping DB: 1234 tests, 567 files, 45678 mappings
🔍 Collected 1250 total tests from pytest
⚠️  Found 16 unmapped test(s)

================================================================================
Building coverage mapping for 16 unmapped test(s)
================================================================================

  Running unmapped test: unit_tests/test_new_feature.py::test_case_1
    ✓ Mapping updated for: unit_tests/test_new_feature.py::test_case_1
  Running unmapped test: unit_tests/test_new_feature.py::test_case_2
    ✓ Mapping updated for: unit_tests/test_new_feature.py::test_case_2
  ...
✓ Successfully mapped 16 test(s)

================================================================================
Running 3 affected test(s)...
================================================================================
...
```

### Mapping Database

The mapping is stored in `.delta/test_mapping.db` (SQLite) at your repo root:

```sql
CREATE TABLE test_coverage_ranges (
    test_name TEXT NOT NULL,
    file_path TEXT NOT NULL,
    ranges TEXT NOT NULL,
    PRIMARY KEY (test_name, file_path)
);
```

Fast lookups: "Which tests cover file X, line Y?"

## 🔧 Installation

### Prerequisites

- Python 3.8+
- pytest >= 7.0
- pytest-cov >= 4.0
- Git repository

### Step 1: Install Package

Install via pip:

```bash
pip install deltatest-cli
```

### Step 2: Build Mapping Database

```bash
cd ~/workspace/myproject

# Build mapping database (resumable)
delta build-mapping --verbose
```

### Step 3: Install Pre-Commit Hook

```bash
cd ~/workspace/myproject
delta install
```

Done! Now every commit will run only affected tests.

## 📖 Usage

### Pre-Commit Hook (Automatic)

Just commit normally:

```bash
git add src/my_module.py
git commit -m "Fix bug in authentication"

# Hook runs automatically:
# - Finds tests covering src/my_module.py
# - Runs only those tests
# - Blocks commit if tests fail
# - Combines coverage on success
```

### Manual Test Running

```bash
# Show what tests would run
delta run --dry-run --verbose

# Run affected tests manually
delta run

# Compare against different branch
delta run --base-branch develop

# Pass pytest arguments
delta run -- -x --pdb
```

### Check Mapping Status

You can inspect the status and statistics of the local mapping database and the remote mapping service ([deltatest.dev](https://deltatest.dev)):

```bash
delta status
```

## 🎛️ Commands

### `delta run`

Run affected tests based on changes.

```bash
delta run [OPTIONS] [-- PYTEST_ARGS]

Options:
  --repo-root PATH        Repository root (default: current directory)
  --local, --no-remote    Run locally without connecting to the deltatest.dev remote mapping service
  --base-branch BRANCH    Branch to compare against (default: master)
  --coverage-file PATH    Path to .coverage file
  --dry-run              Show tests without running
  --min-tests N          Minimum tests required
  --explain              Show exactly which tests are affected by which files/lines
  -v, --verbose          Detailed output
```

### `delta build-mapping`

Build test mapping database iteratively.

```bash
delta build-mapping [OPTIONS]

Options:
  --repo-root PATH        Repository root (default: current directory)
  --local, --no-remote    Build mapping database locally without remote deltatest.dev connection
  --mapping-db PATH       Path to mapping database
  --test-dir PATH         Directory containing tests
  -v, --verbose          Detailed output
```

### `delta status`

Show local and remote (deltatest.dev) database status and statistics.

```bash
delta status [OPTIONS]

Options:
  --repo-root PATH        Repository root (default: current directory)
  --mapping-db PATH       Path to mapping database
  -v, --verbose          Detailed output
```

## 🚫 Bypassing the Hook

For urgent commits:

```bash
git commit --no-verify -m "Urgent hotfix"
```

## 🤝 Contributing

Delta is an open-source developer productivity tool.
