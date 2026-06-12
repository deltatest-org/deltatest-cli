# Iterative Mapping Setup Guide

## 🎯 Overview

The Pintest now includes an **iterative mapping builder** that:
- ✅ Runs tests one-by-one to build the mapping database
- ✅ **Resumable** - can continue after interruption or failure
- ✅ **Persistent** - saves progress after every 10 tests
- ✅ Automatically ensures Docker containers and preflight checks
- ✅ Safe for large test suites (20,000+ tests)

## 🚀 Quick Start

### Option 1: Iterative Build (Recommended )

```bash
cd ~/workspace/myproject

# Start the iterative builder
delta build-mapping --repo-root ~/workspace/myproject --verbose
```

This will:
1. Check Docker containers (start if needed)
2. Run preflight scripts (Neo4j, Postgres)
3. Collect all tests
4. Run each test with coverage
5. Update mapping database after each test
6. Save progress every 10 tests

**If interrupted (Ctrl+C) or fails:**

```bash
# Resume from where it left off
delta build-mapping --repo-root ~/workspace/myproject --resume --verbose
```

### Option 2: Batch Build from Existing Coverage (Advanced)

If you already have a `.coverage` file generated with `--cov-context=test`, you can build/update the database directly from it:

```bash
cd ~/workspace/myproject

# Build mapping from coverage
delta update-mapping --repo-root ~/workspace/myproject --verbose
```

---

## 📊 What Happens During Setup

### 1. Pre-checks (Automatic)

```
🔧 Pre-checks...
🐳 Checking Docker containers...
   Starting Docker containers...
   ✓ Docker containers started
🔧 Running preflight checks...
   Running preflight-postgres.sh...
   ✓ preflight-postgres.sh passed
   Running preflight-neo4j.sh...
   ✓ preflight-neo4j.sh passed
   ✓ All preflight checks passed
```

### 2. Test Collection

```
🔍 Collecting tests...
📊 Test Statistics:
   Total tests:     29721
   Already done:    0
   Failed (skipped): 0
   Remaining:       29721
```

### 3. Iterative Execution

```
================================================================================
Running 29721 test(s) to build mapping...
================================================================================
💡 Tip: Press Ctrl+C to stop. You can resume later with --resume

[1/29721] unit_tests/test_auth.py::test_login... ✓
[2/29721] unit_tests/test_auth.py::test_logout... ✓
[3/29721] unit_tests/test_auth.py::test_timeout... ✗ FAILED (skipping)
...
```

Progress is saved every 10 tests automatically.

### 4. Resume After Interruption

```
♻️  Resuming from previous run...
   Already completed: 1523 tests
   Previously failed: 12 tests
   
📊 Test Statistics:
   Total tests:     29721
   Already done:    1523
   Failed (skipped): 12
   Remaining:       28186
```

---

## 🎛️ Commands

### Build Mapping (Iterative)

```bash
# Basic usage
pintest build-mapping

# With options
pintest build-mapping \
    --repo-root ~/workspace/myproject \
    --resume \
    --verbose \
    --batch-size 20
```

**Options:**
- `--resume`: Resume from previous run (reads .mapping_progress.json)
- `--verbose`: Show detailed output for each test
- `--batch-size N`: Save progress every N tests (default: 10)
- `--mapping-db PATH`: Custom mapping database path
- `--progress-file PATH`: Custom progress file path

### Update Mapping (Batch)

If you have an existing `.coverage` file:

```bash
delta update-mapping \
    --repo-root ~/workspace/myproject \
    --verbose
```

---

## 🐛 Troubleshooting

### Progress Files

The iterative builder creates a progress file at `.mapping_progress.json`:

```json
{
  "completed": ["test1", "test2", ...],
  "failed": ["test_that_failed", ...],
  "start_time": "2026-04-27T18:30:00",
  "last_update": "2026-04-27T19:15:23"
}
```

**Reset progress:**

```bash
cd ~/workspace/myproject
rm .mapping_progress.json
delta build-mapping --repo-root ~/workspace/myproject
```

### Docker Containers Not Running

The builder automatically starts Docker containers. If this fails:

```bash
# Manual start
cd ~/workspace/myproject
docker-compose -f docker-compose-ut.yml up -d

# Check status
docker-compose -f docker-compose-ut.yml ps
```

### Preflight Scripts Failing

```bash
# Run manually
cd ~/workspace/myproject
./scripts/preflight-postgres.sh
./scripts/preflight-neo4j.sh
```

### Many Tests Failing

Failed tests are skipped and recorded. The mapping database is still built for passing tests:

```
📈 Final Statistics:
   Successfully mapped: 28450
   Failed (skipped):    1271
```

You can review failed tests in `.mapping_progress.json` under the `"failed"` key.

---

## ⚡ Performance

### Large Test Suite Example

| Metric | Iterative | Batch |
|--------|-----------|-------|
| Total time | ~2-4 hours | ~45-90 minutes |
| Resumable | ✅ Yes | ❌ No |
| Memory usage | Low (1 test at a time) | High (all tests) |
| Fail-safe | ✅ Continues on failure | ❌ Stops on failure |
| Progress tracking | ✅ Real-time | ❌ None |

**Recommendation:** Use **iterative** for initial setup, **batch** for periodic updates.

---

## 🔄 After Setup

Once the mapping database is built, commits become fast:

```bash
cd ~/workspace/myproject

# Make a change
vim src/auth.py

# Commit (pre-commit hook runs only affected tests)
git add src/auth.py
git commit -m "Fix auth bug"

# Hook output:
# 📝 Found 1 changed Python file
# 📊 Mapping DB: 28450 tests, 1234 files, 456789 mappings
# 📍 src/auth.py: 3 test(s) cover changed lines
# 
# Running 3 affected test(s)...
# ✅ All affected tests passed. Commit allowed.
```

Only 3 tests run instead of 30,000! 🚀

---

## 📝 Best Practices

1. **Initial setup**: Use iterative builder
   ```bash
   delta build-mapping --resume --verbose
   ```

2. **Weekly updates**: Run build-mapping
   ```bash
   delta build-mapping --verbose
   ```

3. **After adding many tests**: Use iterative with resume
   ```bash
   delta build-mapping --resume
   ```

4. **Overnight builds**: Run in background with nohup
   ```bash
   nohup delta build-mapping --verbose > build.log 2>&1 &
   tail -f build.log  # Monitor progress
   ```

5. **Keep progress file**: Don't delete `.mapping_progress.json` until build completes

---

## 🎓 Summary

**Before you commit (one-time setup):**
```bash
cd ~/workspace/myproject
delta build-mapping --verbose
# Wait for completion (2-4 hours, resumable)
```

**After setup:**
```bash
git commit -m "..." 
# Pre-commit hook runs only affected tests (fast!)
```

**Maintenance:**
```bash
# Weekly or after major changes
delta build-mapping --verbose
```

That's it! Delta is now fully configured and ready to use. 🎉
