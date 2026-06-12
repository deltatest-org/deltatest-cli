import os
import subprocess
import pytest
import sys

# Clean up any inherited GIT_* environment variables from git hooks (like pre-commit)
# to prevent tests from modifying/corrupting the parent repository.
for key in list(os.environ.keys()):
    if key.startswith("GIT_"):
        del os.environ[key]

@pytest.fixture
def dummy_repo(tmp_path):
    # 1. Create a dummy Python project in tmp_path
    repo_dir = tmp_path / "dummy_repo"
    repo_dir.mkdir()
    
    # 2. Initialize git
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True, capture_output=True)
    
    # 3. Create delta.toml
    delta_toml = repo_dir / "delta.toml"
    delta_toml.write_text("""
[cloud]
enabled = false
[local]
test_dir = "tests"
""")

    # 4. Create source files
    src_dir = repo_dir / "src"
    src_dir.mkdir()
    
    math_py = src_dir / "math_utils.py"
    math_py.write_text("""def add(a, b):
    return a + b

def subtract(a, b):
    return a - b
""")

    string_py = src_dir / "string_utils.py"
    string_py.write_text("""def to_upper(s):
    return s.upper()
""")

    # 5. Create test files
    tests_dir = repo_dir / "tests"
    tests_dir.mkdir()
    
    test_math_py = tests_dir / "test_math.py"
    test_math_py.write_text("""import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from src.math_utils import add, subtract

def test_add():
    assert add(1, 2) == 3

def test_subtract():
    assert subtract(2, 1) == 1
""")

    test_string_py = tests_dir / "test_string.py"
    test_string_py.write_text("""import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from src.string_utils import to_upper

def test_to_upper():
    assert to_upper("a") == "A"
""")

    # 6. Commit everything
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_dir, check=True, capture_output=True)
    
    return repo_dir

def run_delta(args, cwd):
    env = os.environ.copy()
    env["HOME"] = str(cwd)  # Prevent reading global ~/.delta/config.toml
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    env["PYTHONPATH"] = repo_root + (os.pathsep + env["PYTHONPATH"] if "PYTHONPATH" in env else "")
    cmd = [sys.executable, "-m", "delta.cli"] + args
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env)
    return result

def test_delta_track_and_run(dummy_repo):
    # Scenario A (Baseline): Run delta build-mapping
    res = run_delta(["build-mapping"], cwd=dummy_repo)
    assert res.returncode == 0
    
    # Check if mapping db is created
    db_path = dummy_repo / ".delta" / "test_mapping.db"
    assert db_path.exists()
    
    # Verify nothing to run on clean working tree
    res = run_delta(["run", "--dry-run", "--base-branch=main"], cwd=dummy_repo)
    assert res.returncode == 0
    # Expected: "Would run 0 test" or "No tests to run!" or "Running 0 selected"
    # Actually, current code outputs "Would run 0 test(s)" if test_selection is not empty but dry-run is provided?
    # No, cli.py has `if not test_selection: print("No tests to run!"); return 0`
    assert "No tests to run" in res.stdout or "0 test(s)" in res.stdout

    # Scenario C (True Positive): Modify a tracked line
    math_py = dummy_repo / "src" / "math_utils.py"
    content = math_py.read_text()
    # Change add function
    content = content.replace("return a + b", "return a + b + 0")
    math_py.write_text(content)
    
    res = run_delta(["run", "--dry-run", "--base-branch=main", "-v"], cwd=dummy_repo)
    print("STDOUT:", res.stdout)
    print("STDERR:", res.stderr)
    assert res.returncode == 0
    assert "tests/test_math.py::test_add" in res.stdout or "test_math.py" in res.stdout
    assert "test_string.py" not in res.stdout
    
    # Revert math.py
    subprocess.run(["git", "checkout", "--", "src/math_utils.py"], cwd=dummy_repo, check=True)

    # Scenario D (True Negative): Add a comment
    content = math_py.read_text()
    content += "\n# This is a comment\n"
    math_py.write_text(content)
    
    res = run_delta(["run", "--dry-run", "--base-branch=main"], cwd=dummy_repo)
    assert res.returncode == 0
    # The comment line was never executed, so adding it shouldn't trigger any tests.
    assert "No tests to run" in res.stdout or "0 test(s)" in res.stdout

    # Revert math.py
    subprocess.run(["git", "checkout", "--", "src/math_utils.py"], cwd=dummy_repo, check=True)

    # Scenario E (New Test Discovery): Add a new test file
    test_new_py = dummy_repo / "tests" / "test_new.py"
    test_new_py.write_text("""def test_something_new():\n    assert True\n""")
    subprocess.run(["git", "add", "tests/test_new.py"], cwd=dummy_repo, check=True)
    
    res = run_delta(["run", "--dry-run", "--base-branch=main"], cwd=dummy_repo)
    assert res.returncode == 0
    assert "test_new.py" in res.stdout
    
    # Remove test_new.py and unstage
    subprocess.run(["git", "rm", "-f", "tests/test_new.py"], cwd=dummy_repo, check=True)

    # Scenario F (Deleted File): Delete a test file and modify related source
    os.remove(dummy_repo / "tests" / "test_string.py")
    subprocess.run(["git", "add", "."], cwd=dummy_repo, check=True)
    subprocess.run(["git", "commit", "-m", "Remove test_string.py"], cwd=dummy_repo, check=True)
    
    # Modify string_utils.py which used to be covered by the deleted test
    string_py = dummy_repo / "src" / "string_utils.py"
    content = string_py.read_text()
    content = content.replace("return s.upper()", "return s.upper() + ''")
    string_py.write_text(content)
    
    res = run_delta(["run", "--dry-run", "--base-branch=main"], cwd=dummy_repo)
    assert res.returncode == 0
    # test_string_py is gone. delta run shouldn't crash trying to find it.
    assert "Exception" not in res.stderr
    assert "Traceback" not in res.stderr


def test_delta_chunking_and_fail_fast(dummy_repo):
    from delta.cli import DeltaRunner
    runner = DeltaRunner(dummy_repo, verbose=True)
    
    # Generate 1005 dummy test names
    dummy_tests = {f"tests/test_math.py::test_dummy_{i}" for i in range(1005)}
    
    # Run with fail-fast (-x)
    # Since these dummy tests don't exist, pytest will fail on the first chunk.
    # DeltaRunner should catch the failure and abort the second chunk.
    import io
    from unittest.mock import patch
    
    # Capture stderr to verify the abort message
    stderr_buf = io.StringIO()
    with patch("sys.stderr", stderr_buf):
        returncode = runner.run_tests(dummy_tests, dry_run=False, verbose=True, pytest_args=["-x"])
    
    assert returncode != 0
    stderr_output = stderr_buf.getvalue()
    assert "Fail-fast enabled" in stderr_output
    assert "Aborting remaining 1 chunk(s)" in stderr_output


def test_delta_install(dummy_repo):
    # Verify pre-commit hook does not exist yet
    hooks_dir = dummy_repo / ".git" / "hooks"
    pre_commit = hooks_dir / "pre-commit"
    post_commit = hooks_dir / "post-commit"
    
    assert not pre_commit.exists()
    assert not post_commit.exists()

    # Run installation command
    res = run_delta(["install", "pre_commit", "--repo-root", str(dummy_repo)], cwd=dummy_repo)
    assert res.returncode == 0
    assert "Pre-commit hook installed successfully" in res.stdout
    assert "Post-commit hook installed successfully" not in res.stdout
    assert not post_commit.exists()

    # Verify hooks created and are executable
    assert pre_commit.exists()
    
    if os.name != 'nt':
        assert os.access(str(pre_commit), os.X_OK)

    # Verify .gitignore was updated
    gitignore = dummy_repo / ".gitignore"
    assert gitignore.exists()
    gitignore_content = gitignore.read_text()
    assert ".test_mapping.db" in gitignore_content
    assert ".delta/" in gitignore_content

    # Run installation command again to test backup creation
    res = run_delta(["install", "pre_commit", "--repo-root", str(dummy_repo)], cwd=dummy_repo)
    assert res.returncode == 0
    assert "Backing up existing pre-commit hook" in res.stdout
    assert (hooks_dir / "pre-commit.backup").exists()

    # Test post-commit hook cleanup and backup restoration
    # Create a deprecated Delta post-commit hook
    post_commit.write_text("#!/bin/bash\n# Delta Post-Commit Hook\nsome_cmd")
    # Create a user backup
    post_commit_backup = hooks_dir / "post-commit.backup"
    post_commit_backup.write_text("user original hook")

    # Run install again
    res = run_delta(["install", "pre_commit", "--repo-root", str(dummy_repo)], cwd=dummy_repo)
    assert res.returncode == 0
    assert "Removed deprecated Delta post-commit hook" in res.stdout
    assert "Restored previous post-commit hook backup" in res.stdout
    
    # Check that post-commit has been restored to user original hook
    assert post_commit.exists()
    assert post_commit.read_text() == "user original hook"
    assert not post_commit_backup.exists()

    # Test the alias command as well
    # Remove existing files to start clean
    for f in [pre_commit, post_commit, hooks_dir / "pre-commit.backup", post_commit_backup]:
        if f.exists():
            f.unlink()

    res = run_delta(["install-pre-commit", "--repo-root", str(dummy_repo)], cwd=dummy_repo)
    assert res.returncode == 0
    assert pre_commit.exists()
    assert not post_commit.exists()


def test_delta_base_branch_resolution(dummy_repo):
    import pytest
    # Test GitDiffParser._resolve_base_branch directly
    from delta.git_diff_parser import GitDiffParser
    parser = GitDiffParser(dummy_repo)
    
    # 'main' exists, so resolving 'main' should return 'main'
    assert parser._resolve_base_branch("main") == "main"
    
    # 'master' does not exist in dummy_repo, so resolving it should raise ValueError
    with pytest.raises(ValueError, match="Target branch 'master' does not exist"):
        parser._resolve_base_branch("master")
    
    # Non-existent branch like 'foobar' should also raise ValueError
    with pytest.raises(ValueError, match="Target branch 'foobar' does not exist"):
        parser._resolve_base_branch("foobar")

    # Now verify that running delta run with default base branch (master) fails cleanly on a repo that only has 'main'
    # Build mapping first so database exists
    res = run_delta(["build-mapping"], cwd=dummy_repo)
    assert res.returncode == 0

    res = run_delta(["run", "--dry-run"], cwd=dummy_repo)
    assert res.returncode == 1
    assert "Error: Target branch 'master' does not exist" in res.stderr


def test_delta_local_flag(dummy_repo):
    # Test delta build-mapping --local
    res = run_delta(["build-mapping", "--local"], cwd=dummy_repo)
    assert res.returncode == 0
    db_path = dummy_repo / ".delta" / "test_mapping.db"
    assert db_path.exists()

    # Test delta run --local --dry-run
    res = run_delta(["run", "--local", "--dry-run", "--base-branch=main"], cwd=dummy_repo)
    assert res.returncode == 0
    assert "No tests to run" in res.stdout or "0 test(s)" in res.stdout
    
    # Test alias --no-remote for both
    res = run_delta(["build-mapping", "--no-remote"], cwd=dummy_repo)
    assert res.returncode == 0
    
    res = run_delta(["run", "--no-remote", "--dry-run", "--base-branch=main"], cwd=dummy_repo)
    assert res.returncode == 0


def test_delta_explain(dummy_repo):
    # Build mapping first so database exists
    res = run_delta(["build-mapping", "--local"], cwd=dummy_repo)
    assert res.returncode == 0

    # Test delta run --local --explain --dry-run
    res = run_delta(["run", "--local", "--explain", "--dry-run", "--base-branch=main"], cwd=dummy_repo)
    assert res.returncode == 0
    assert "Delta Test Selection Explanation" in res.stdout


def test_delta_skipped_tests(dummy_repo):
    import json
    # Write a dummy skipped test to the skipped_tests.json file
    delta_dir = dummy_repo / ".delta"
    delta_dir.mkdir(parents=True, exist_ok=True)
    skipped_file = delta_dir / "skipped_tests.json"
    with open(skipped_file, "w") as f:
        # Save a test name that exists in the repo or dummy test
        json.dump(["tests/test_math.py::test_add"], f)

    # Build mapping locally so it imports the skipped test list
    res = run_delta(["build-mapping", "--local"], cwd=dummy_repo)
    assert res.returncode == 0

    # Ensure the skipped test is recorded in DB under __skipped__
    from delta.test_mapping_db_v2 import TestMappingDBV2
    with TestMappingDBV2(delta_dir / "test_mapping.db") as db:
        cursor = db.conn.cursor()
        cursor.execute("SELECT test_name FROM test_coverage_ranges WHERE file_path = '__skipped__'")
        rows = cursor.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "tests/test_math.py::test_add"

    # Now make a change to math_utils.py which would ordinarily trigger all math tests
    math_py = dummy_repo / "src" / "math_utils.py"
    content = math_py.read_text()
    content = content.replace("return a + b", "return a + b + 0")
    math_py.write_text(content)

    # Run delta run and check that the skipped test is bypassed
    res = run_delta(["run", "--local", "--dry-run", "--base-branch=main"], cwd=dummy_repo)
    assert res.returncode == 0
    assert "Skipping 1 known-skipped test(s)" in res.stdout


def test_delta_run_incremental_chunk_update(dummy_repo):
    from delta.cli import DeltaRunner
    from unittest.mock import patch, MagicMock
    
    db_path = dummy_repo / ".delta" / "test_mapping.db"
    
    # Initialize the mapping database first
    from delta.test_mapping_db_v2 import TestMappingDBV2
    with TestMappingDBV2(db_path) as db:
        db.initialize_schema()
        
    runner = DeltaRunner(dummy_repo, mapping_db=TestMappingDBV2(db_path), verbose=True)
    
    # Generate 1005 dummy tests to force chunking (chunk_size is 1000)
    dummy_tests = {f"tests/test_math.py::test_dummy_{i}" for i in range(1005)}
    
    # We want to verify that update_mapping is called after each chunk.
    # Let's mock subprocess.run and update_mapping.
    with patch("subprocess.run") as mock_run, \
         patch("delta.update_mapping.update_mapping") as mock_update_mapping:
         
        mock_run.return_value = MagicMock(returncode=0)
        
        # Run tests
        returncode = runner.run_tests(dummy_tests, dry_run=False, verbose=True)
        
        assert returncode == 0
        # There should be 2 chunks (1000 and 5)
        assert mock_run.call_count == 2
        # update_mapping should be called twice with incremental=True
        assert mock_update_mapping.call_count == 2
        
        # Check call arguments
        for call in mock_update_mapping.call_args_list:
            assert call.kwargs.get("incremental") is True
            assert call.kwargs.get("mapping_db") == db_path


def test_delta_version(dummy_repo):
    from delta import __version__
    res = run_delta(["--version"], cwd=dummy_repo)
    assert res.returncode == 0
    assert f"delta {__version__}" in res.stdout or f"delta {__version__}" in res.stderr
    
    res = run_delta(["-V"], cwd=dummy_repo)
    assert res.returncode == 0
    assert f"delta {__version__}" in res.stdout or f"delta {__version__}" in res.stderr


def test_delta_run_local_no_remote_connection(dummy_repo):
    from unittest.mock import patch, MagicMock
    from delta.cli import cmd_run
    import argparse
    
    # Build mapping locally first so database exists
    db_path = dummy_repo / ".delta" / "test_mapping.db"
    from delta.test_mapping_db_v2 import TestMappingDBV2
    with TestMappingDBV2(db_path) as db:
        db.initialize_schema()
        
    from delta.config import Config, CloudConfig
    mock_config = Config(
        cloud=CloudConfig(
            api_key="test_api_key",
            api_url="https://api.test-deltatest.dev",
            repo_id="test_repo_id",
            branch="main"
        )
    )
    
    # Create fake parsed arguments as argparse.Namespace with local=True
    args = argparse.Namespace(
        repo_root=dummy_repo,
        local=True,
        mapping_db=db_path,
        base_branch="main",
        verbose=True,
        unknown_args=[],
        test_dir=None,
        min_tests=0,
        dry_run=True
    )
    
    with patch("delta.cli.Config.load", return_value=mock_config), \
         patch("delta.cli.CloudMappingDB") as mock_cloud_db, \
         patch("delta.cli.DeltaRunner") as mock_runner:
         
         mock_runner.return_value.find_affected_tests.return_value = set()
         mock_runner.return_value.run_tests.return_value = 0
         
         # Call cmd_run
         try:
             cmd_run(args)
         except SystemExit as e:
             assert e.code == 0
         
         # Verify CloudMappingDB was NOT instantiated
         mock_cloud_db.assert_not_called()


def test_delta_status(dummy_repo):
    # Run status on uninitialized repo
    res = run_delta(["status"], cwd=dummy_repo)
    assert res.returncode == 0
    assert "Database status: Not initialized" in res.stdout
    assert "Integration:  Disabled" in res.stdout

    # Build mapping locally first
    res_build = run_delta(["build-mapping", "--local"], cwd=dummy_repo)
    assert res_build.returncode == 0

    # Run status again to verify local stats
    res = run_delta(["status"], cwd=dummy_repo)
    assert res.returncode == 0
    assert "Database file:" in res.stdout
    assert "Total mapped tests:  " in res.stdout
    assert "Total mapped files:  " in res.stdout

    # Now verify with cloud settings mocked
    from unittest.mock import patch
    from delta.config import Config, CloudConfig
    mock_config = Config(
        cloud=CloudConfig(
            api_key="test_api_key",
            api_url="https://api.test-deltatest.dev",
            repo_id="test_repo_id",
            branch="main"
        )
    )

    from delta.cli import cmd_status
    import argparse
    db_path = dummy_repo / ".delta" / "test_mapping.db"
    
    args = argparse.Namespace(
        repo_root=dummy_repo,
        mapping_db=db_path,
        verbose=True
    )

    with patch("delta.cli.Config.load", return_value=mock_config), \
         patch("delta.cli.CloudMappingDB") as mock_cloud_db:
         
         # Mock cloud stats
         mock_cloud_db.return_value.get_stats.return_value = {
             "total_tests": 123,
             "files_covered": 45
         }
         
         try:
             cmd_status(args)
         except SystemExit as e:
             assert e.code == 0

         mock_cloud_db.return_value.get_stats.assert_called_once()
