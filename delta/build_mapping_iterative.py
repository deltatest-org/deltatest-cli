#!/usr/bin/env python3
"""
Iteratively build test mapping database by running tests one-by-one.

This script is designed to be resumable - if it fails or is interrupted,
you can run it again and it will continue from where it left off.

Usage:
    python build_mapping_iterative.py --repo-root ~/workspace/myproject
    
    # Continue after interruption:
    python build_mapping_iterative.py --repo-root ~/workspace/myproject --resume
"""

import argparse
import subprocess
import sys
import json
from pathlib import Path
from datetime import datetime
from typing import Set

# Add delta to path
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from delta.test_mapping_db_v2 import TestMappingDBV2
from delta.pre_commit_hook import (
    collect_all_tests,
    find_unmapped_tests,
)


def run_test_chunk_with_mapping(
    repo_root: Path,
    test_names: list,
    mapping_db_path: Path,
    verbose: bool = False,
    pytest_args: list = None
) -> tuple[Set[str], Set[str]]:
    """
    Run a chunk of tests with coverage and update mapping.
    
    Args:
        repo_root: Repository root directory
        test_names: List of test names to run
        mapping_db_path: Path to mapping database
        verbose: Print verbose output
        pytest_args: Additional pytest arguments
    
    Returns:
        Tuple of (passed_tests, failed_tests)
    """
    # Run chunk of tests with coverage
    cmd = [
        sys.executable, "-m", "pytest",
        "-p", "delta.pytest_plugin",
        "--cov",
        "--cov-context=test",
        "--cov-append",
        "--cov-report=",
        "-v",
        "--tb=short",
    ]
    if pytest_args:
        cmd.extend(pytest_args)
    
    if len(test_names) > 1000:
        delta_dir = repo_root / ".delta"
        delta_dir.mkdir(parents=True, exist_ok=True)
        select_file = delta_dir / "xdist_select.json"
        with open(select_file, "w") as f:
            json.dump(test_names, f)
        cmd.extend(["--delta-select-file", str(select_file)])
    else:
        cmd.extend(test_names)
    
    print(f"   Running: {' '.join(cmd[:6])} ... {len(test_names)} tests", flush=True)
    
    import os
    env = os.environ.copy()
    env["DELTA_DISABLE"] = "1"
    
    result = subprocess.run(
        cmd,
        cwd=repo_root,
        text=True,
        env=env
    )
    
    # Parse pytest exit code
    # 0 = all passed, 1 = some failed, 2+ = errors/interrupted
    passed = set()
    failed = set()
    
    if result.returncode == 0:
        # All tests passed
        passed = set(test_names)
    else:
        # Some failed - for mapping purposes, we still got coverage
        # Mark all as "passed" (meaning: we have coverage data for them)
        passed = set(test_names)
    
    # Find and use coverage data files
    coverage_file = repo_root / ".coverage"
    if not coverage_file.exists() and (repo_root / "coverage" / ".coverage").exists():
        coverage_file = repo_root / "coverage" / ".coverage"
    coverage_chunks = list(repo_root.glob(".coverage.*")) + list((repo_root / "coverage").glob(".coverage.*"))
    
    # Filter out empty files
    valid_chunks = []
    for chunk_file in coverage_chunks:
        try:
            import sqlite3
            conn = sqlite3.connect(chunk_file)
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row[0] for row in cursor.fetchall()]
            conn.close()
            if tables:  # Has tables, so it's valid
                valid_chunks.append(chunk_file)
        except:
            pass  # Skip invalid files
    
    # Try to combine if needed
    if valid_chunks:
        combine_cmd = [sys.executable, "-m", "coverage", "combine"]
        subprocess.run(
            combine_cmd,
            cwd=repo_root,
            text=True
        )
    
    # Update mapping database - try .coverage first, then fall back to chunks
    try:
        import_source = None
        
        if coverage_file.exists():
            # Check if .coverage is valid (has tables)
            import sqlite3
            try:
                conn = sqlite3.connect(coverage_file)
                cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                tables = [row[0] for row in cursor.fetchall()]
                conn.close()
                if tables:
                    import_source = coverage_file
            except:
                pass
        
        # If .coverage is empty/invalid, use the largest chunk file
        if not import_source and valid_chunks:
            # Sort by size, use largest
            import_source = max(valid_chunks, key=lambda f: f.stat().st_size)
            if verbose:
                print(f"    Using chunk file: {import_source.name}")
        
        if import_source:
            with TestMappingDBV2(mapping_db_path) as db:
                db.import_from_coverage(import_source, incremental=True, repo_root=repo_root)
        else:
            if verbose:
                print(f"    No valid coverage file found")
    except Exception as e:
        if verbose:
            print(f"    Could not update mapping: {e}")
    
    return passed, failed


def build_mapping_iteratively(
    repo_root: Path,
    mapping_db: Path,
    test_dir: str = "tests",
    verbose: bool = False,
    pytest_args: list = None,
    no_remote: bool = False,
    subprocess_mode: bool = False
):
    """
    Build mapping database by running unmapped tests.
    Uses database queries to determine which tests still need mapping.
    Resumable by design - just run again and it continues from where it left off.
    
    Args:
        repo_root: Repository root directory
        mapping_db: Path to mapping database
        test_dir: Test directory to collect from (default: "unit_tests")
        verbose: Print verbose output
        pytest_args: Additional pytest arguments
        subprocess_mode: Enable subprocess coverage tracking
    """
    repo_root = repo_root.resolve()
    
    if subprocess_mode:
        import sysconfig
        import os
        print("Enabling subprocess coverage tracking...")
        # 1. Write sitecustomize.py bootstrap hook
        try:
            site_packages = sysconfig.get_paths().get("purelib")
            if site_packages:
                sp_path = Path(site_packages)
                sitecustomize_file = sp_path / "sitecustomize.py"
                hook_code = (
                    "\n# site-packages/sitecustomize.py (written by delta build-mapping --subprocess)\n"
                    "import coverage\n"
                    "coverage.process_startup()\n"
                )
                if sitecustomize_file.exists():
                    content = sitecustomize_file.read_text()
                    if "coverage.process_startup()" not in content:
                        sitecustomize_file.write_text(content + hook_code)
                        print(f"   Appended startup hook to {sitecustomize_file}")
                else:
                    sitecustomize_file.write_text(hook_code)
                    print(f"   Created startup hook at {sitecustomize_file}")
        except Exception as e:
            print(f"   Warning: Could not install sitecustomize.py hook: {e}")

        # 2. Write/update .coveragerc
        try:
            coveragerc_file = repo_root / ".coveragerc"
            import configparser
            config_parser = configparser.ConfigParser()
            if coveragerc_file.exists():
                config_parser.read(coveragerc_file)
            if not config_parser.has_section("run"):
                config_parser.add_section("run")
            config_parser.set("run", "parallel", "True")
            config_parser.set("run", "concurrency", "multiprocessing")
            with open(coveragerc_file, "w") as f:
                config_parser.write(f)
            print(f"   Updated {coveragerc_file} with parallel=True and concurrency=multiprocessing")
        except Exception as e:
            print(f"   Warning: Could not update .coveragerc: {e}")

        # 3. Write env variable to .delta/.env
        try:
            delta_dir = repo_root / ".delta"
            delta_dir.mkdir(parents=True, exist_ok=True)
            env_file = delta_dir / ".env"
            coveragerc_path = repo_root / ".coveragerc"
            
            existing_vars = {}
            if env_file.exists():
                for line in env_file.read_text().splitlines():
                    if "=" in line and not line.strip().startswith("#"):
                        k, v = line.split("=", 1)
                        existing_vars[k.strip()] = v.strip()
            
            existing_vars["COVERAGE_PROCESS_START"] = f'"{coveragerc_path}"'
            
            with open(env_file, "w") as f:
                for k, v in existing_vars.items():
                    f.write(f"{k}={v}\n")
            print(f"   Wrote COVERAGE_PROCESS_START to {env_file}")
            
            os.environ["COVERAGE_PROCESS_START"] = str(coveragerc_path)
        except Exception as e:
            print(f"   Warning: Could not write env file: {e}")
    
    print("Test Mapping Builder")
    print("=" * 80)
    print(f"Repository: {repo_root}")
    print(f"Mapping DB: {mapping_db}")
    
    # Initialize mapping database
    with TestMappingDBV2(mapping_db) as db:
        db.initialize_schema()
    
    # ── Cloud/Remote detection for finding unmapped tests ────────────────
    from delta.config import Config
    cfg = Config.load()
    use_cloud = cfg.is_cloud_enabled and bool(cfg.cloud.repo_id) and not no_remote
    
    mapping_db_obj = mapping_db
    if use_cloud:
        try:
            from delta.cloud_mapping_db import CloudMappingDB
            mapping_db_obj = CloudMappingDB(cfg.cloud)
            if verbose:
                print(f"Connecting to remote mapping to find unmapped tests (repo {cfg.cloud.repo_id[:8]}...)", flush=True)
        except Exception as e:
            if verbose:
                print(f"Could not connect to remote mapping, falling back to local database: {e}", flush=True)
            mapping_db_obj = mapping_db

    # Find unmapped tests by querying database
    print("\nFinding unmapped tests...")
    unmapped_tests = find_unmapped_tests(repo_root, mapping_db_obj, test_dir, verbose)
    
    if not unmapped_tests:
        print("\nAll tests already mapped!")
        return 0
    
    # Determine if we need chunking (OS ARG_MAX limit protection)
    remaining_list = sorted(unmapped_tests)
    total_tests = len(remaining_list)
    chunk_size = 1000  # Safe threshold to avoid "Argument list too long"
    
    has_xdist = pytest_args and any(
        arg == '-n' or arg.startswith('-n=') or arg == '--numprocesses' or arg.startswith('--numprocesses=')
        for arg in pytest_args
    )
    needs_chunking = total_tests > chunk_size and not has_xdist
    
    print(f"\nTest Statistics:")
    print(f"   Unmapped tests: {total_tests}")
    
    print(f"\n{'='*80}")
    if needs_chunking:
        num_chunks = (total_tests + chunk_size - 1) // chunk_size
        print(f"Running {total_tests} tests in {num_chunks} chunks of up to {chunk_size}...")
        print(f"Each chunk updates the database immediately - safe to interrupt/resume")
    else:
        print(f"Running {total_tests} test(s) at once...")
    print(f"{'='*80}")
    print()
    
    # Run tests (in chunks if needed)
    all_passed = set()
    all_failed = set()
    
    try:
        if needs_chunking:
            # Run in chunks
            for i in range(0, total_tests, chunk_size):
                chunk = remaining_list[i:i + chunk_size]
                chunk_num = (i // chunk_size) + 1
                total_chunks = (total_tests + chunk_size - 1) // chunk_size
                
                print(f"Chunk {chunk_num}/{total_chunks}: Running {len(chunk)} tests...", flush=True)
                
                try:
                    passed, failed = run_test_chunk_with_mapping(
                        repo_root,
                        chunk,
                        mapping_db,
                        verbose,
                        pytest_args
                    )
                    
                    # Track for final statistics
                    all_passed.update(passed)
                    all_failed.update(failed)
                    
                    print(f"      Chunk {chunk_num} complete: {len(passed)} passed, {len(failed)} failed", flush=True)
                    
                except Exception as e:
                    print(f"   Chunk {chunk_num} failed: {e}", flush=True)
                    # Continue with next chunk
                    continue
        else:
            # Run all at once (small enough)
            print(f"Running {len(remaining_list)} tests with coverage...", flush=True)
            
            passed, failed = run_test_chunk_with_mapping(
                repo_root,
                remaining_list,
                mapping_db,
                verbose,
                pytest_args
            )
            
            # Track for final statistics
            all_passed.update(passed)
            all_failed.update(failed)
            
            print(f"\n   {len(passed)} passed,    {len(failed)} failed")
    
    except KeyboardInterrupt:
        print(f"\n\nInterrupted by user!")
        print(f"Next time you run, it will automatically continue from where it left off")
        print(f"   (unmapped tests are determined by querying the database)")
        return 130
    
    # Final summary
    print(f"\n{'='*80}")
    print("Mapping build complete!")
    print(f"{'='*80}")
    print(f"\nFinal Statistics:")
    print(f"   Successfully mapped: {len(all_passed)} tests")
    print(f"   Failed (skipped):    {len(all_failed)} tests")
    
    # Report failed tests
    if all_failed:
        print(f"\nFailed Tests ({len(all_failed)}):")
        for test_name in sorted(all_failed)[:10]:  # Show first 10
            print(f"   - {test_name}")
        if len(all_failed) > 10:
            print(f"   ... and {len(all_failed) - 10} more")
    
    # Show mapping DB stats
    with TestMappingDBV2(mapping_db) as db:
        stats = db.get_stats()
        print(f"\nMapping Database:")
        print(f"   Total tests:    {stats['total_tests']}")
        print(f"   Total files:    {stats['total_files']}")
        print(f"   Total mappings: {stats['total_mappings']}")
    
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Iteratively build test mapping database"
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Repository root directory (default: current directory)"
    )
    parser.add_argument(
        "--mapping-db",
        type=Path,
        help="Path to mapping database (default: <repo>/.delta/test_mapping.db)"
    )
    parser.add_argument(
        "--test-dir",
        type=str,
        default="unit_tests",
        help="Test directory to collect from (default: unit_tests from pytest.ini)"
    )
    parser.add_argument(
        "--local", "--no-remote",
        action="store_true",
        help="Do not connect to remote mapping, only build coverage locally"
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose output"
    )
    
    args, unknown = parser.parse_known_args()
    pytest_args = unknown
    if pytest_args and pytest_args[0] == '--':
        pytest_args = pytest_args[1:]
    
    repo_root = args.repo_root.resolve()
    if args.mapping_db:
        mapping_db = args.mapping_db
    else:
        mapping_db = repo_root / ".delta" / "test_mapping.db"
        legacy_db = repo_root / ".test_mapping.db"
        if not mapping_db.exists() and legacy_db.exists():
            mapping_db.parent.mkdir(parents=True, exist_ok=True)
            try:
                legacy_db.rename(mapping_db)
            except Exception:
                mapping_db = legacy_db
        else:
            mapping_db.parent.mkdir(parents=True, exist_ok=True)
    
    return build_mapping_iteratively(
        repo_root,
        mapping_db,
        args.test_dir,
        args.verbose,
        pytest_args=pytest_args,
        no_remote=getattr(args, 'local', False)
    )


if __name__ == "__main__":
    sys.exit(main())
