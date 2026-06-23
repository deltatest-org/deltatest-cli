#!/usr/bin/env python3
"""
Pre-commit hook for Delta.

Runs only tests affected by code changes, with coverage enabled.
Combines new coverage with existing .coverage file.
"""

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Set, Optional
import os
from datetime import datetime

# Add delta to path
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from delta.git_diff_parser import GitDiffParser
from delta.test_mapping_db_v2 import TestMappingDBV2
from delta.config import Config


class TeeOutput:
    """Write output to both stdout and a log file."""
    
    def __init__(self, log_file: Path):
        self.log_file = log_file
        self.stdout = sys.stdout
        
    def __enter__(self):
        # Open log file in append mode
        self.file = open(self.log_file, 'a', encoding='utf-8')
        
        # Write session header
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        header = f"\n{'='*80}\nPre-commit run: {timestamp}\n{'='*80}\n"
        self.file.write(header)
        self.file.flush()
        
        # Replace sys.stdout
        sys.stdout = self
        return self
    
    def __exit__(self, *args):
        # Restore original stdout
        sys.stdout = self.stdout
        
        # Write footer and close file
        footer = f"{'='*80}\n"
        self.file.write(footer)
        self.file.flush()
        self.file.close()
    
    def write(self, text):
        """Write to both stdout and log file."""
        self.stdout.write(text)
        self.stdout.flush()
        self.file.write(text)
        self.file.flush()
    
    def flush(self):
        """Flush both outputs."""
        self.stdout.flush()
        self.file.flush()


def ensure_git_lfs(repo_root: Path, verbose: bool = False) -> bool:
    """
    Ensure Git LFS is initialized in the repository.
    
    This is needed if the repo uses Git LFS for storing large files like .delta/test_mapping.db.
    Safe to run multiple times.
    
    Args:
        repo_root: Repository root directory
        verbose: Print verbose output
        
    Returns:
        True if Git LFS is available and initialized, False otherwise
    """
    # Check if git-lfs is installed
    result = subprocess.run(
        ["git", "lfs", "version"],
        cwd=repo_root,
        capture_output=True,
        text=True
    )
    
    if result.returncode != 0:
        if verbose:
            print("Git LFS not installed (optional - only needed for pre-built databases)")
        return True  # Not a failure - just not available
    
    if verbose:
        print("Initializing Git LFS...", flush=True)
    
    # Initialize Git LFS (safe to run multiple times)
    result = subprocess.run(
        ["git", "lfs", "install"],
        cwd=repo_root,
        capture_output=not verbose,
        text=True
    )
    
    if result.returncode == 0:
        if verbose:
            print("      Git LFS initialized")
        return True
    else:
        if verbose:
            print(f"   Git LFS initialization warning: {result.stderr}")
        return True  # Not critical - continue anyway


def should_exclude_test(test_name: str) -> bool:
    """
    Check if a test should be excluded from runs and mapping.
    
    Args:
        test_name: Test name or path
        
    Returns:
        True if test should be excluded
    """
    # Exclude patterns
    exclude_patterns = [
        'test_mypy.py',  # Skip mypy type checking tests
    ]
    
    for pattern in exclude_patterns:
        if pattern in test_name:
            return True
    
    return False


def normalize_file_path(file_path: str) -> str:
    """
    Normalize source file path to match database format.
    
    Strips the 'src/' prefix that git diff includes but coverage.py doesn't store.
    
    Args:
        file_path: File path from git diff (may have src/ prefix)
        
    Returns:
        Normalized file path without src/ prefix
    """
    if file_path.startswith('src/'):
        return file_path[4:]  # Strip 'src/'
    return file_path


def strip_test_parameters(test_name: str) -> str:
    """
    Strip parametrized test arguments from test name.
    
    For example:
        'test_foo.py::test_bar[arg1-arg2]' -> 'test_foo.py::test_bar'
        'test_foo.py::test_bar' -> 'test_foo.py::test_bar'
    
    Args:
        test_name: Test name possibly with parameters in brackets
        
    Returns:
        Test name without parameters
    """
    # Strip parametrization by splitting on the first '['
    if "[" in test_name:
        return test_name.split("[")[0]
    return test_name


def normalize_test_path(test_name: str) -> str:
    """
    Normalize test path to match database format.
    
    Strips common prefixes like 'unit_tests/', 'tests/', etc. to ensure
    collected test names match what's stored in the coverage database.
    
    Args:
        test_name: Test name with potential prefix
        
    Returns:
        Normalized test name without prefix
    """
    # Common test directory prefixes to strip
    prefixes = ['unit_tests/', 'tests/', 'test/', './']
    
    for prefix in prefixes:
        if test_name.startswith(prefix):
            return test_name[len(prefix):]
    
    return test_name


def denormalize_test_path(test_name: str, repo_root: Path) -> str:
    """
    Convert normalized test path back to pytest-compatible path.
    
    Adds back the 'unit_tests/' or 'tests/' prefix by checking which exists.
    
    Args:
        test_name: Normalized test name (without prefix)
        repo_root: Repository root directory
        
    Returns:
        Full pytest-compatible test path
    """
    # If already has a prefix, return as-is
    if test_name.startswith(('unit_tests/', 'tests/', 'test/', './')):
        return test_name
    
    # Try common test directory prefixes, including auto-detected subdirectories
    prefixes = ['unit_tests/', 'tests/', 'test/']
    for cand in ['tests', 'unit_tests', 'test']:
        for path in repo_root.glob(f"*/{cand}"):
            if path.is_dir() and not any(p in path.parts for p in [".git", ".venv", "venv", "build", "dist"]):
                rel = str(path.relative_to(repo_root)) + "/"
                if rel not in prefixes:
                    prefixes.append(rel)
    
    for prefix in prefixes:
        # Extract file path from test name (before ::)
        file_part = test_name.split('::')[0]
        full_path = repo_root / prefix / file_part
        
        if full_path.exists():
            return prefix + test_name
    
    # If no prefix works, return normalized name (pytest will error, but at least we tried)
    return test_name


def collect_all_tests(
    repo_root: Path,
    test_dir: str = None,
    verbose: bool = False,
    pytest_args: list = None
) -> Set[str]:
    """
    Collect all available tests using pytest --collect-only.
    
    Args:
        repo_root: Repository root directory
        test_dir: Test directory to collect from (default: auto-detect 'tests' or 'unit_tests')
        verbose: Print verbose output
        pytest_args: Additional arguments passed to pytest to check for targets
        
    Returns:
        Set of all test names in the format "path/to/test.py::TestClass::test_method"
        (excluding tests that should be skipped)
    """
    test_targets = []
    if pytest_args:
        for arg in pytest_args:
            # Skip flags/options (e.g. starting with -)
            if arg.startswith("-"):
                continue
            path = Path(arg)
            if not path.is_absolute():
                path = repo_root / path
            if path.exists():
                test_targets.append(arg)
                
    if test_targets:
        if verbose:
            print(f"Using pytest argument targets for collection: {test_targets}", flush=True)
    else:
        if test_dir is None:
            # Auto-detect test directory
            for candidate in ["tests", "unit_tests", "test"]:
                if (repo_root / candidate).exists():
                    test_dir = candidate
                    break
            if test_dir is None:
                # Search one level deep for common test directories
                found = False
                for cand in ["tests", "unit_tests", "test"]:
                    for path in repo_root.glob(f"*/{cand}"):
                        if path.is_dir() and not any(p in path.parts for p in [".git", ".venv", "venv", "build", "dist"]):
                            test_dir = str(path.relative_to(repo_root))
                            found = True
                            break
                    if found:
                        break
                if test_dir is None:
                    test_dir = "tests"
        test_targets = [test_dir]

    try:
        # Use --quiet to get node IDs only
        cmd = [sys.executable, "-m", "pytest", "--collect-only", "-q"] + test_targets
        result = subprocess.run(
            cmd,
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )
        
        # Parse output - pytest --collect-only -q -q outputs test node IDs
        test_names = set()
        excluded_count = 0
        
        for line in result.stdout.split('\n'):
            line = line.strip()
            # Skip empty lines and summary lines
            if not line or 'collected' in line.lower() or 'skipped' in line.lower():
                continue
            # Lines like "path/to/test.py::test_function" are test node IDs
            if '::' in line:
                # Check if test should be excluded
                if should_exclude_test(line):
                    excluded_count += 1
                    continue
                
                # Add the test node ID
                test_names.add(line)
        
        if verbose:
            print(f"Collected {len(test_names)} total tests from pytest", flush=True)
            if excluded_count > 0:
                print(f"   Excluded {excluded_count} tests (test_mypy.py, etc.)", flush=True)
        
        return test_names
        
    except subprocess.TimeoutExpired:
        print(f"\nTest collection timed out after 120s!", file=sys.stderr)
        print(f"   Try narrowing the test directory (e.g. --test-dir tests/)", file=sys.stderr)
        return set()
    except subprocess.CalledProcessError as e:
        print(f"\nPytest failed to collect tests!", file=sys.stderr)
        print(f"This usually means your project dependencies aren't installed.", file=sys.stderr)
        if e.stderr:
            print(f"\nPytest Error Output:\n{e.stderr}", file=sys.stderr)
        elif e.stdout:
            print(f"\nPytest Output:\n{e.stdout}", file=sys.stderr)
        return set()


def get_mapped_tests(db_or_path, verbose: bool = False) -> Set[str]:
    """
    Get all tests currently in the mapping database.
    
    Args:
        db_or_path: Path to database or initialized DB object
        verbose: Print verbose output
        
    Returns:
        Set of test names from the database
    """
    if hasattr(db_or_path, 'get_all_test_names'):
        try:
            if hasattr(db_or_path, 'conn') and db_or_path.conn is None:
                with db_or_path as db:
                    mapped_tests = db.get_all_test_names()
            else:
                mapped_tests = db_or_path.get_all_test_names()
            if verbose:
                print(f"Found {len(mapped_tests)} tests in mapping database", flush=True)
            return mapped_tests
        except Exception as e:
            if verbose:
                print(f"Warning: Could not query mapping database: {e}", file=sys.stderr)
            # Fall back to file path approach below if it's a TestMappingDBV2
            if hasattr(db_or_path, 'db_path'):
                db_or_path = db_or_path.db_path
            else:
                return set()

    if not isinstance(db_or_path, Path) or not db_or_path.exists():
        if verbose:
            print(f"Warning: Mapping database not found: {db_or_path}", file=sys.stderr)
        return set()
    
    try:
        with TestMappingDBV2(db_or_path) as db:
            mapped_tests = db.get_all_test_names()
            
            if verbose:
                print(f"Found {len(mapped_tests)} tests in mapping database", flush=True)
                
            return mapped_tests
            
    except Exception as e:
        if verbose:
            print(f"Warning: Could not query mapping database: {e}", file=sys.stderr)
        return set()


def find_unmapped_tests(
    repo_root: Path,
    db_or_path,
    test_dir: str = None,
    verbose: bool = False,
    pytest_args: list = None
) -> Set[str]:
    """
    Find tests that exist in the codebase but aren't in the mapping database yet.
    
    Args:
        repo_root: Repository root directory
        db_or_path: Path to test mapping database or initialized DB object
        test_dir: Test directory to collect from (default: "unit_tests")
        verbose: Print verbose output
        pytest_args: Additional arguments passed to pytest to check for targets
        
    Returns:
        Set of unmapped test names (with original paths suitable for pytest)
    """
    all_tests = collect_all_tests(repo_root, test_dir, verbose, pytest_args)
    mapped_tests = get_mapped_tests(db_or_path, verbose)
    
    # Normalize both sides for comparison (comparing by base test name without parameters)
    # Build a mapping from normalized name (without params) -> list of original names
    from collections import defaultdict
    normalized_to_originals = defaultdict(list)
    for t in all_tests:
        # Strip parameters and normalize path
        test_without_params = strip_test_parameters(t)
        norm_key = normalize_test_path(test_without_params)
        # Also strip conftest-collected subdirectories for better matching
        # e.g. "tests/subdir/test_foo.py::test_bar" -> "test_foo.py::test_bar"
        norm_key = norm_key.split("/")[-1] if "/" in norm_key and "::" in norm_key else norm_key
        # Keep track of all original test names (with params and prefix)
        normalized_to_originals[norm_key].append(t)
    
    # For mapped tests, also strip parameters and normalize
    normalized_mapped = set()
    for t in mapped_tests:
        test_without_params = strip_test_parameters(t)
        norm_key = normalize_test_path(test_without_params)
        # Apply same normalization as above
        norm_key = norm_key.split("/")[-1] if "/" in norm_key and "::" in norm_key else norm_key
        normalized_mapped.add(norm_key)
    
    # Find normalized names that aren't mapped
    unmapped_normalized = set(normalized_to_originals.keys()) - normalized_mapped
    
    # Convert back to original names (with unit_tests/ prefix) for pytest
    # Include all parameterized versions of unmapped tests
    unmapped = set()
    for norm in unmapped_normalized:
        unmapped.update(normalized_to_originals[norm])
    
    if verbose:
        print(f"\nTest discovery stats:", flush=True)
        print(f"   Total tests found: {len(all_tests)}", flush=True)
        print(f"   Already mapped: {len(mapped_tests)}", flush=True)
        print(f"   Unmapped (need to run): {len(unmapped)}", flush=True)
    
    if verbose and unmapped:
        print(f"\nFound {len(unmapped)} unmapped tests (not in mapping DB)", flush=True)
        print("   These will be run individually to build coverage mapping", flush=True)
        for test in sorted(list(unmapped)[:5]):
            print(f"    - {test}", flush=True)
        if len(unmapped) > 5:
            print(f"    ... and {len(unmapped) - 5} more", flush=True)
    
    return unmapped


def run_test_chunk_with_mapping_update(
    repo_root: Path,
    test_names: list,
    mapping_db_path: Path,
    verbose: bool = False,
    pytest_args: list = None
) -> tuple[Set[str], Set[str], bool]:
    """
    Run tests with coverage and update the mapping database.
    
    This is used for unmapped tests to build the coverage mapping.
    Output flows directly to the terminal so users can see test failures.
    
    Args:
        repo_root: Repository root directory
        test_names: List of test names to run
        mapping_db_path: Path to test mapping database
        verbose: Print verbose output
        
    Returns:
        Tuple of (passed_tests, failed_tests, all_passed)
        Note: failed_tests will be empty since pytest output shows failures directly
    """
    from .test_mapping_db_v2 import normalize_test_name
    
    # Normalize test names to remove parametrization
    # This prevents issues with dynamic parameters (lambdas, complex objects)
    # Running the base test name (test_foo) will automatically run all parametrizations
    normalized_tests = list(set(normalize_test_name(t) for t in test_names))
    
    # Run tests with coverage - output flows directly to terminal
    cmd = [
        sys.executable, "-m", "pytest",
        "-p", "delta.pytest_plugin",
        "--cov",
        "--cov-context=test",
        "--cov-append",
        "--cov-report=",
        "-v",  # Always verbose so users see test failures
        "--tb=short",  # Short traceback format
    ]
    if pytest_args:
        cmd.extend(pytest_args)
    import json
    delta_dir = repo_root / ".delta"
    delta_dir.mkdir(parents=True, exist_ok=True)
    select_file = delta_dir / "xdist_select.json"
    with open(select_file, "w") as f:
        json.dump(normalized_tests, f)
    cmd.extend(["--delta-select-file", str(select_file)])
    
    result = subprocess.run(
        cmd,
        cwd=repo_root
    )
    
    # Determine passed/failed from exit code
    # pytest returns 0 if all passed, non-zero if any failed
    if result.returncode == 0:
        passed = set(test_names)
        failed = set()
    else:
        # Some failed - we'll mark all as "run" but return the failed status
        # The pytest output above already showed which ones failed
        passed = set(test_names)
        failed = set()  # We don't track individual failures since output was shown
    
    # Update mapping database with new coverage
    # This happens REGARDLESS of whether tests passed or failed
    print(f"\n{'─'*80}", flush=True)
    print(f"Updating mapping database after test run...", flush=True)
    
    try:
        coverage_file = repo_root / ".coverage"
        if not coverage_file.exists() and (repo_root / "coverage" / ".coverage").exists():
            coverage_file = repo_root / "coverage" / ".coverage"
        if not coverage_file.exists():
            print(f"    No coverage file found at {coverage_file}", flush=True)
            print(f"    Tests may have crashed before generating coverage data.", flush=True)
            return passed, failed, result.returncode == 0
        
        print(f"       Coverage file found: {coverage_file}", flush=True)
        
        # Get DB stats before import
        tests_before = 0
        tests_after = 0
        tests_added = 0
        
        with TestMappingDBV2(mapping_db_path) as db:
            stats_before = db.get_stats()
            tests_before = stats_before['total_tests']
            
            print(f"    Database before import: {tests_before} tests", flush=True)
            
            # Import coverage
            db.import_from_coverage(coverage_file, incremental=True, repo_root=repo_root)
            
            # Get stats after import
            stats_after = db.get_stats()
            tests_after = stats_after['total_tests']
            tests_added = tests_after - tests_before
            
            print(f"    Database after import (pre-commit): {tests_after} tests", flush=True)
            
            # Explicitly commit to ensure data is persisted
            db.conn.commit()
            print(f"       Commit executed", flush=True)
        
        # Verify commit persisted by opening a new connection
        with TestMappingDBV2(mapping_db_path) as db_verify:
            stats_verify = db_verify.get_stats()
            tests_verify = stats_verify['total_tests']
            print(f"    Database verification (new connection): {tests_verify} tests", flush=True)
            
            if tests_verify != tests_after:
                print(f"    WARNING: Commit may have failed! Expected {tests_after}, got {tests_verify}", flush=True)
        
        print(f"    Mapping updated: {tests_added} new tests added to DB (ran {len(test_names)}, DB now has {tests_after})", flush=True)
        
        if tests_added < len(test_names) * 0.8:  # Less than 80% were new
            print(f"    WARNING: Only {tests_added}/{len(test_names)} tests were new to the database!", flush=True)
            print(f"    This might indicate duplicates or tests already mapped.", flush=True)
        
        # Move coverage file to backup to avoid conflicts in next chunk
        coverage_backup = repo_root / f".coverage.chunk_{len(test_names)}"
        if coverage_backup.exists():
            coverage_backup.unlink()
        coverage_file.rename(coverage_backup)
        print(f"       Coverage backed up to {coverage_backup.name}", flush=True)
        print(f"{'─'*80}", flush=True)
        
    except Exception as e:
        print(f"    Could not update mapping: {e}", flush=True)
        print(f"{'─'*80}", flush=True)
        import traceback
        if verbose:
            print(f"    Full traceback:", flush=True)
            traceback.print_exc()
    
    # Return success/failure based on exit code
    # Note: We return empty failed set since pytest output already showed failures
    return passed, failed, result.returncode == 0


def run_single_test_with_mapping_update(
    repo_root: Path,
    test_name: str,
    mapping_db_path: Path,
    verbose: bool = False
) -> bool:
    """
    Run a single test with coverage and update the mapping database.
    
    This is used for unmapped tests to gradually build the coverage mapping.
    
    Args:
        repo_root: Repository root directory
        test_name: Test name to run
        mapping_db_path: Path to test mapping database
        verbose: Print verbose output
        
    Returns:
        True if test passed and mapping updated, False otherwise
    """
    from .test_mapping_db_v2 import normalize_test_name
    
    # Normalize test name to remove parametrization
    normalized_test = normalize_test_name(test_name)
    
    if verbose:
        print(f"\n  Running unmapped test: {normalized_test}")
    
    # Run single test with coverage
    cmd = [
        "python", "-m", "pytest",
        "-p", "delta.pytest_plugin",
        "--cov",
        "--cov-context=test",
        "--cov-append",
        "--cov-report=",
        "-v",
        normalized_test
    ]
    
    result = subprocess.run(
        cmd,
        cwd=repo_root,
        capture_output=True,
        text=True
    )
    
    if result.returncode != 0:
        if verbose:
            print(f"       Test failed: {test_name}")
        return False
    
    # Update mapping database with new coverage
    try:
        coverage_file = repo_root / ".coverage"
        if not coverage_file.exists() and (repo_root / "coverage" / ".coverage").exists():
            coverage_file = repo_root / "coverage" / ".coverage"
        if coverage_file.exists():
            with TestMappingDBV2(mapping_db_path) as db:
                db.import_from_coverage(coverage_file, incremental=True, repo_root=repo_root)
            if verbose:
                print(f"       Mapping updated for: {test_name}")
        return True
    except Exception as e:
        if verbose:
            print(f"    Could not update mapping: {e}")
        return True  # Still return True since test passed


def run_unmapped_tests_iteratively(
    repo_root: Path,
    unmapped_tests: Set[str],
    mapping_db_path: Path,
    verbose: bool = False,
    chunk_size: int = 1000,
    pytest_args: list = None
) -> tuple[bool, int]:
    """
    Run unmapped tests with automatic chunking to avoid OS argument limits.
    
    Args:
        repo_root: Repository root directory
        unmapped_tests: Set of unmapped test names
        mapping_db_path: Path to test mapping database
        verbose: Print verbose output
        chunk_size: Maximum tests per chunk (default 1000 to avoid ARG_MAX)
        
    Returns:
        Tuple of (all_passed, count_run)
    """
    if not unmapped_tests:
        return True, 0
    
    test_list = sorted(unmapped_tests)
    total_tests = len(test_list)
    
    # Determine if we need to chunk (OS has ARG_MAX limit)
    # Safe threshold: 1000 tests per chunk to avoid "Argument list too long"
    has_xdist = pytest_args and any(
        arg == '-n' or arg.startswith('-n=') or arg == '--numprocesses' or arg.startswith('--numprocesses=')
        for arg in pytest_args
    )
    needs_chunking = total_tests > chunk_size and not has_xdist
    
    print(f"\n{'='*80}", flush=True)
    print(f"Building coverage mapping for {total_tests} unmapped test(s)", flush=True)
    if needs_chunking:
        num_chunks = (total_tests + chunk_size - 1) // chunk_size
        print(f"Running in {num_chunks} chunks of up to {chunk_size} tests...", flush=True)
        print(f"Each chunk updates the database immediately - safe to interrupt/resume", flush=True)
    else:
        print(f"Running all tests at once...", flush=True)
    print(f"{'='*80}", flush=True)
    
    # Run tests in chunks
    all_passed = True
    count_run = 0
    
    if needs_chunking:
        # Break into chunks
        for i in range(0, total_tests, chunk_size):
            chunk = test_list[i:i + chunk_size]
            chunk_num = (i // chunk_size) + 1
            total_chunks = (total_tests + chunk_size - 1) // chunk_size
            
            # Verify chunk tests are actually unmapped
            if verbose:
                with TestMappingDBV2(mapping_db_path) as db:
                    cursor = db.conn.cursor()
                    already_mapped = []
                    for test in chunk[:10]:  # Check first 10 tests
                        cursor.execute("SELECT COUNT(*) FROM test_coverage_ranges WHERE test_name = ?", (test,))
                        if cursor.fetchone()[0] > 0:
                            already_mapped.append(test)
                    if already_mapped:
                        print(f"   WARNING: {len(already_mapped)}/10 sampled tests already in DB:", flush=True)
                        for t in already_mapped[:3]:
                            print(f"      - {t}", flush=True)
            
            print(f"\nChunk {chunk_num}/{total_chunks}: Running {len(chunk)} tests (mapped so far: {count_run})...", flush=True)
            
            passed, failed, chunk_passed = run_test_chunk_with_mapping_update(
                repo_root,
                chunk,
                mapping_db_path,
                verbose,
                pytest_args
            )
            
            count_run += len(chunk)
            
            if not chunk_passed:
                all_passed = False
                print(f"   Some tests in chunk {chunk_num} failed (continuing...)", flush=True)
            else:
                print(f"      Chunk {chunk_num} passed - {count_run}/{total_tests} tests mapped", flush=True)
            
            # Verify tests were actually added to database
            with TestMappingDBV2(mapping_db_path) as db:
                stats = db.get_stats()
                if verbose:
                    print(f"   Database now has {stats['total_tests']} tests, {stats['total_mappings']} mappings", flush=True)
    else:
        # Run all at once
        print(f"\nRunning {total_tests} unmapped tests with coverage...", flush=True)
        
        passed, failed, all_passed = run_test_chunk_with_mapping_update(
            repo_root,
            test_list,
            mapping_db_path,
            verbose,
            pytest_args
        )
        
        count_run = total_tests
    
    if all_passed:
        print(f"\nSuccessfully mapped {count_run} test(s)", flush=True)
    else:
        print(f"\nSome unmapped tests failed (see output above)", flush=True)
        print(f"   The test failures are shown in the pytest output above.", flush=True)
        print(f"   All {count_run} tests were still added to the mapping database.", flush=True)
    
    # Clean up backup coverage files
    for backup in repo_root.glob(".coverage.chunk_*"):
        try:
            backup.unlink()
            if verbose:
                print(f"   Cleaned up {backup.name}", flush=True)
        except Exception as e:
            if verbose:
                print(f"   Could not remove {backup.name}: {e}", flush=True)
    
    return all_passed, count_run


def get_unstaged_test_files(repo_root: Path) -> Set[str]:
    """
    Find test files in the current commit (staged changes).
    
    Returns:
        Set of test file paths that are being committed
    """
    try:
        # Get staged files
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=d"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True
        )
        
        staged_files = result.stdout.strip().split('\n')
        
        # Filter for test files
        test_files = set()
        for file_path in staged_files:
            if file_path.strip():
                # Check if it's a test file
                if 'test_' in file_path or '_test.py' in file_path or '/tests/' in file_path:
                    # Exclude test_mypy.py and other excluded patterns
                    if not should_exclude_test(file_path):
                        test_files.add(file_path)
        
        return test_files
        
    except subprocess.CalledProcessError as e:
        print(f"Warning: Could not get staged test files: {e.stderr}", file=sys.stderr)
        return set()


def get_new_tests_in_commit(repo_root: Path) -> Set[str]:
    """
    Find NEW test files being added in this commit.
    
    Returns:
        Set of new test file paths
    """
    try:
        # Get added files (new files)
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=A"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True
        )
        
        new_files = result.stdout.strip().split('\n')
        
        # Filter for test files
        new_test_files = set()
        for file_path in new_files:
            if file_path.strip():
                if 'test_' in file_path or '_test.py' in file_path or '/tests/' in file_path:
                    # Exclude test_mypy.py and other excluded patterns
                    if not should_exclude_test(file_path):
                        new_test_files.add(file_path)
        
        return new_test_files
        
    except subprocess.CalledProcessError as e:
        print(f"Warning: Could not get new test files: {e.stderr}", file=sys.stderr)
        return set()


def combine_coverage_files(repo_root: Path, new_coverage: Path):
    """
    Combine new coverage data with existing .coverage file.
    
    Args:
        repo_root: Repository root directory
        new_coverage: Path to new coverage file from test run
    """
    existing_coverage = repo_root / ".coverage"
    if not existing_coverage.exists() and (repo_root / "coverage" / ".coverage").exists():
        existing_coverage = repo_root / "coverage" / ".coverage"
    
    if not new_coverage.exists():
        print(f"Warning: New coverage file not found: {new_coverage}", file=sys.stderr)
        return
    
    if not existing_coverage.exists():
        # No existing coverage, just rename new one
        print("No existing coverage file, using new coverage as baseline")
        new_coverage.rename(existing_coverage)
        return
    
    # Use coverage combine to merge
    print("Combining coverage data with existing .coverage...")
    
    try:
        # Backup existing coverage
        backup = repo_root / ".coverage.backup"
        if backup.exists():
            backup.unlink()
        
        existing_coverage.rename(backup)
        
        # Move new coverage to a temporary name for combining
        temp_new = repo_root / ".coverage.new"
        if temp_new.exists():
            temp_new.unlink()
        new_coverage.rename(temp_new)
        
        # Use coverage combine
        result = subprocess.run(
            [sys.executable, "-m", "coverage", "combine", str(backup), str(temp_new)],
            cwd=repo_root,
            capture_output=True,
            text=True
        )
        
        if result.returncode != 0:
            print(f"Warning: Coverage combine failed: {result.stderr}", file=sys.stderr)
            # Restore backup
            backup.rename(existing_coverage)
            temp_new.rename(new_coverage)
        else:
            print("   Coverage data combined successfully")
            # Clean up
            if backup.exists():
                backup.unlink()
            if temp_new.exists():
                temp_new.unlink()
                
    except Exception as e:
        print(f"Error combining coverage: {e}", file=sys.stderr)
        # Try to restore backup
        backup = repo_root / ".coverage.backup"
        if backup.exists() and not existing_coverage.exists():
            backup.rename(existing_coverage)


def find_affected_tests(
    repo_root: Path,
    target_branch: str,
    mapping_db_path: Path,
    verbose: bool = False
) -> Set[str]:
    """
    Find tests affected by staged changes.
    
    Args:
        repo_root: Repository root
        target_branch: Target branch to compare against
        mapping_db_path: Path to test mapping SQLite database
        verbose: Print verbose output
        
    Returns:
        Set of test names to run
    """
    # Initialize git parser
    git_parser = GitDiffParser(repo_root)
    
    # Get diff using GitDiffParser which handles merge-base resolution properly
    try:
        diff_output, merge_base = git_parser.get_diff(target_branch)
    except (ValueError, RuntimeError) as e:
        print(f"Error getting diff: {e}", file=sys.stderr)
        return set()
    
    # Parse diff
    changes = git_parser.parse_diff(diff_output)
    python_changes = git_parser.filter_python_files(changes)
    
    if verbose:
        print(f"\nFound {len(python_changes)} changed Python files", flush=True)
        if python_changes:
            for file_path in sorted(python_changes.keys()):
                print(f"   • {file_path}", flush=True)
    
    # Early exit if no Python changes
    if not python_changes:
        if verbose:
            print("No Python files changed. No tests to run.", flush=True)
        return set()
    
    # Get changed test files (always run these)
    changed_test_files = git_parser.get_changed_test_files(python_changes)
    
    if verbose and changed_test_files:
        print(f"Changed test files: {len(changed_test_files)}", flush=True)
        for test_file in sorted(changed_test_files):
            print(f"  - {test_file}", flush=True)
    
    # Find tests from mapping database
    affected_tests = set()
    
    if not mapping_db_path.exists():
        print(f"Warning: Test mapping database not found: {mapping_db_path}", file=sys.stderr)
        print("   Run: delta update-mapping", file=sys.stderr)
        # Fall back to running only changed test files
        return changed_test_files
    
    # Query database for affected tests
    with TestMappingDBV2(mapping_db_path) as db:
        stats = db.get_stats()
        if verbose:
            print(f"Mapping DB: {stats['total_tests']} tests, {stats['total_files']} files, {stats['total_mappings']} mappings", flush=True)
        
        for file_path, change in python_changes.items():
            # Skip test files (we already have them)
            if file_path in changed_test_files:
                continue
            
            changed_lines = change.get_all_changed_lines()
            if not changed_lines:
                continue
            
            # Normalize file path to match database format (strip src/ prefix)
            normalized_path = normalize_file_path(file_path)
            
            # Find tests covering these lines
            tests = db.find_tests_for_file_lines(normalized_path, changed_lines)
            affected_tests.update(tests)
            
            if verbose and tests:
                print(f"{file_path}: {len(tests)} test(s) cover changed lines")
                for test in sorted(tests)[:3]:
                    print(f"    - {test}")
                if len(tests) > 3:
                    print(f"    ... and {len(tests) - 3} more")
    
    # Always include changed test files
    all_tests = affected_tests | changed_test_files
    
    # Add new test files (always run new tests)
    new_tests = get_new_tests_in_commit(repo_root)
    if new_tests:
        if verbose:
            print(f"New test files (always run): {len(new_tests)}")
            for test_file in sorted(new_tests):
                print(f"  + {test_file}")
        all_tests.update(new_tests)
    
    # Filter out excluded tests (test_mypy.py, etc.)
    filtered_tests = {test for test in all_tests if not should_exclude_test(test)}
    excluded_count = len(all_tests) - len(filtered_tests)
    
    if verbose and excluded_count > 0:
        print(f"Excluded {excluded_count} tests (test_mypy.py, etc.)", flush=True)
    
    return filtered_tests


def run_tests_with_coverage(
    repo_root: Path,
    test_paths: Set[str],
    pytest_args: list = None,
    verbose: bool = False
) -> bool:
    """
    Run tests with coverage enabled.
    
    Args:
        repo_root: Repository root
        test_paths: Paths to test files/modules to run (may be normalized)
        pytest_args: Additional pytest arguments
        verbose: Print verbose output
        
    Returns:
        True if tests passed, False otherwise
    """
    from .test_mapping_db_v2 import normalize_test_name
    
    if not test_paths:
        print("   No tests to run")
        return True
    
    # Denormalize test paths for pytest (add back unit_tests/ prefix if needed)
    denormalized_paths = {denormalize_test_path(test, repo_root) for test in test_paths}
    
    # Separate file paths from specific test node IDs
    test_files = set()
    test_node_ids = set()
    
    for path in denormalized_paths:
        if "::" in path:
            # This is a specific test node ID (e.g., test_file.py::test_name[param])
            # Normalize to remove parametrization - this handles dynamic parameters
            base_path = path.split("::")[0]
            test_name_with_params = "::".join(path.split("::")[1:])
            
            # Normalize the test name (removes [params])
            normalized_test_name = normalize_test_name(test_name_with_params)
            normalized_path = f"{base_path}::{normalized_test_name}"
            
            test_node_ids.add(normalized_path)
        else:
            # This is a test file path
            test_files.add(path)
    
    # Build pytest command
    # Auto-detect coverage source (check for common source dirs, default to no --cov= filter)
    # Users can override with pytest_args like --cov=my_package
    cov_source = []
    for candidate in ["src", "app", "lib"]:
        if (repo_root / candidate).exists():
            cov_source = [f"--cov={candidate}"]
            break
    cmd = [
        sys.executable, "-m", "pytest",
        "-p", "delta.pytest_plugin",
    ] + cov_source + [
        "--cov-context=test",
        "--cov-append",  # Append to existing coverage
        "--cov-report=",  # No report output (we'll combine later)
        "-v"  # Verbose
    ]
    
    # Add custom pytest args
    if pytest_args:
        cmd.extend(pytest_args)
    
    # Add test files and node IDs
    # Combine test files and node IDs into a list for the select file
    all_test_selectors = list(test_files | test_node_ids) or list(test_paths)
    import json
    delta_dir = repo_root / ".delta"
    delta_dir.mkdir(parents=True, exist_ok=True)
    select_file = delta_dir / "xdist_select.json"
    with open(select_file, "w") as f:
        json.dump(all_test_selectors, f)
    cmd.extend(["--delta-select-file", str(select_file)])
    
    if verbose:
        print(f"\nRunning command: {' '.join(cmd)}")
        if test_node_ids:
            print(f"   Note: Parametrized tests will run all parameter combinations")
    
    print(f"\n{'='*80}", flush=True)
    print(f"Running {len(test_paths)} affected test(s)...", flush=True)
    print(f"{'='*80}\n", flush=True)
    
    # Run tests
    result = subprocess.run(
        cmd,
        cwd=repo_root
    )
    
    return result.returncode == 0


def main():
    parser = argparse.ArgumentParser(
        description="Delta for pre-commit hook"
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Repository root directory (default: current directory)"
    )
    parser.add_argument(
        "--target-branch",
        "--base-branch",  # Alias for backward compatibility
        default="HEAD",
        help="Target branch to compare against (default: HEAD - current branch)"
    )
    parser.add_argument(
        "--mapping-db",
        type=Path,
        default=None,
        help="Path to test mapping database (default: <repo>/.delta/test_mapping.db)"
    )
    parser.add_argument(
        "--test-dir",
        type=str,
        default=None,
        help="Test directory to collect from (default: auto-detect tests/ or unit_tests/)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show which tests would run without actually running them"
    )
    parser.add_argument(
        "--skip-coverage-combine",
        action="store_true",
        help="Don't combine coverage with existing .coverage file"
    )
    parser.add_argument(
        "--skip-unmapped",
        action="store_true",
        help="Skip searching for and running unmapped tests (only run affected mapped tests)"
    )
    parser.add_argument(
        "--local", "--no-remote",
        action="store_true",
        help="Do not connect to remote mapping, only build coverage/run tests locally"
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose output"
    )
    parser.add_argument(
        "pytest_args",
        nargs="*",
        help="Additional arguments to pass to pytest"
    )
    
    args = parser.parse_args()
    
    repo_root = args.repo_root.resolve()
    
    # Set up log file for tracking all output
    delta_dir = repo_root / ".delta"
    delta_dir.mkdir(parents=True, exist_ok=True)
    log_file = delta_dir / "pre-commit.log"
    
    # Use TeeOutput to write to both stdout and log file
    with TeeOutput(log_file):
        # Default mapping database location
        if args.mapping_db is None:
            mapping_db = delta_dir / "test_mapping.db"
            legacy_db = repo_root / ".test_mapping.db"
            if not mapping_db.exists() and legacy_db.exists():
                try:
                    legacy_db.rename(mapping_db)
                except Exception:
                    mapping_db = legacy_db
        else:
            mapping_db = args.mapping_db
        
        if args.verbose:
            print(f"Repository: {repo_root}", flush=True)
            print(f"Target branch: {args.target_branch}", flush=True)
            print(f"Log file: {log_file}", flush=True)

        # ── Cloud mode detection ─────────────────────────────────
        cloud_cfg = Config.load()
        use_cloud = cloud_cfg.is_cloud_enabled and bool(cloud_cfg.cloud.repo_id) and not args.local
        cloud_db = None

        if use_cloud:
            from delta.cloud_mapping_db import CloudMappingDB
            cloud_db = CloudMappingDB(cloud_cfg.cloud)
            mapping_db_obj = cloud_db
            if args.verbose:
                print(f"Mapping Service: Delta Cloud (repo {cloud_cfg.cloud.repo_id[:8]}...)", flush=True)
        else:
            mapping_db_obj = mapping_db
            if args.verbose:
                print(f"Local mode: {mapping_db}", flush=True)

        
        # Initialize Git LFS only if using local DB (cloud doesn't need it)
        if not use_cloud:
            ensure_git_lfs(repo_root, args.verbose)
        
        # Check if mapping database exists and is properly initialized (if not using cloud)
        needs_rebuild = False
        if not use_cloud:
            if not mapping_db.exists():
                needs_rebuild = True
            else:
                # Check if database is properly initialized (has tables)
                try:
                    with TestMappingDBV2(mapping_db) as db:
                        if not db.is_initialized():
                            needs_rebuild = True
                            print(f"Database file exists but is not initialized", flush=True)
                except Exception as e:
                    needs_rebuild = True
                    print(f"Database appears corrupted: {e}", flush=True)
        
        if needs_rebuild:
            print(f"\nTest mapping database not found or invalid: {mapping_db}", flush=True)
            print("Building mapping database for the first time...", flush=True)
            print("   This is a one-time operation that may take 30-60 minutes.", flush=True)
            print("   Future commits will be fast.", flush=True)
            print("   Safe to interrupt (Ctrl+C) - it will resume automatically next time.", flush=True)
            print()
            
            # Import here to avoid circular dependency
            from .build_mapping_iterative import build_mapping_iteratively
            
            result = build_mapping_iteratively(
                repo_root,
                mapping_db,
                args.test_dir,
                args.verbose,
                pytest_args=args.pytest_args
            )
            
            if result != 0:
                print("\nFailed to build mapping database. Commit blocked.")
                return 1
            
            print("\nMapping database built successfully!", flush=True)
            print()
        
        # Find affected tests
        if use_cloud:
            # Cloud path: send all changed files+lines in one API call
            git_parser = GitDiffParser(repo_root)
            diff_output = git_parser.get_diff(args.target_branch)
            all_changes = git_parser.parse_diff(diff_output)
            py_changes = git_parser.filter_python_files(all_changes)

            cloud_changes = []
            for file_path, change in py_changes.items():
                lines = change.get_all_changed_lines()
                if lines:
                    cloud_changes.append({"file": file_path, "lines": sorted(lines)})

            if cloud_changes:
                affected_tests, _ = mapping_db_obj.find_tests_for_changes(cloud_changes)
            else:
                affected_tests = set()

            # Always include directly changed test files
            changed_test_files = git_parser.get_changed_test_files(py_changes)
            affected_tests |= changed_test_files
        else:
            affected_tests = find_affected_tests(
                repo_root,
                args.target_branch,
                mapping_db,
                args.verbose
            )
        
        # Find unmapped tests (tests that exist but aren't in mapping DB yet)
        unmapped_tests = set()
        if not args.skip_unmapped:
            unmapped_tests = find_unmapped_tests(
                repo_root,
                mapping_db_obj,
                args.test_dir,
                args.verbose,
                pytest_args=args.pytest_args
            )
            
            # Add unmapped tests to affected tests (they should also run)
            if unmapped_tests:
                print(f"\nFound {len(unmapped_tests)} unmapped test(s)", flush=True)
                print("   These tests will run individually to build the coverage mapping", flush=True)
                affected_tests.update(unmapped_tests)
        elif args.verbose:
            print(f"\nSkipping unmapped test search (--skip-unmapped enabled)", flush=True)
        
        if not affected_tests:
            print("\nNo affected tests found. Commit allowed.", flush=True)
            return 0
        
        print(f"\n{'='*80}", flush=True)
        print(f"Found {len(affected_tests)} affected test(s)", flush=True)
        if unmapped_tests:
            print(f"  ({len(unmapped_tests)} unmapped + {len(affected_tests) - len(unmapped_tests)} mapped)", flush=True)
        print(f"{'='*80}", flush=True)
        
        if args.verbose or args.dry_run:
            mapped_tests = affected_tests - unmapped_tests
            if mapped_tests:
                print("\nMapped tests:")
                for test in sorted(mapped_tests):
                    print(f"  • {test}")
            if unmapped_tests:
                print("\nUnmapped tests:")
                for test in sorted(unmapped_tests):
                    print(f"  {test}")
        
        if args.dry_run:
            print("\n[DRY RUN] Would run these tests")
            return 0
        
        # First, run unmapped tests iteratively to build mapping
        if unmapped_tests:
            unmapped_passed, unmapped_count = run_unmapped_tests_iteratively(
                repo_root,
                unmapped_tests,
                mapping_db,
                args.verbose,
                pytest_args=args.pytest_args
            )
            
            if not unmapped_passed:
                print("\nUnmapped tests failed. Commit blocked.", flush=True)
                print("Fix the failing tests and try again.", flush=True)
                return 1
        
        # Then run mapped affected tests (if any remain)
        mapped_affected = affected_tests - unmapped_tests
        tests_passed = True
        
        if mapped_affected:
            tests_passed = run_tests_with_coverage(
                repo_root,
                mapped_affected,
                args.pytest_args,
                args.verbose
            )
        
        if not tests_passed:
            print("\nTests failed. Commit blocked.", flush=True)
            print("Fix the failing tests and try again.", flush=True)
            return 1
        
        # Combine coverage / push to cloud
        if not args.skip_coverage_combine:
            new_coverage = repo_root / ".coverage"
            if new_coverage.exists():
                if use_cloud:
                    run_stats = {
                        "tests_selected": len(affected_tests),
                        "result": "passed",
                    }
                    cloud_db.push_coverage(new_coverage, run_stats, verbose=args.verbose)
                else:
                    combine_coverage_files(repo_root, new_coverage)
        
        # Stage updated mapping database if it was modified
        # This ensures the updated database is included in the current commit
        if not use_cloud and mapping_db.exists():
            # Check if mapping DB was modified (only for local mode)
            result = subprocess.run(
                ["git", "diff", "--name-only", str(mapping_db.name)],
                cwd=repo_root,
                capture_output=True,
                text=True
            )
            
            # Also check if it's untracked
            status_result = subprocess.run(
                ["git", "status", "--porcelain", str(mapping_db.name)],
                cwd=repo_root,
                capture_output=True,
                text=True
            )
            
            if result.stdout.strip() or status_result.stdout.strip():
                if args.verbose:
                    print(f"\nStaging updated mapping database: {mapping_db.name}")
                
                # Add to staging area
                add_result = subprocess.run(
                    ["git", "add", str(mapping_db.name)],
                    cwd=repo_root,
                    capture_output=True,
                    text=True
                )
                
                if add_result.returncode == 0:
                    print(f"   Updated mapping database will be included in this commit")
                elif args.verbose:
                    print(f"Could not stage mapping database: {add_result.stderr}")
        
        print("\nAll affected tests passed. Commit allowed.", flush=True)
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, RuntimeError) as e:
        print(f"\nError: {e}", file=sys.stderr, flush=True)
        sys.exit(1)
