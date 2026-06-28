"""CLI for Delta."""

import argparse
import getpass
import os
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Set

from .git_diff_parser import GitDiffParser
from .coverage_mapper import CoverageMapper
from .config import Config
from .cloud_mapping_db import CloudMappingDB
from .test_mapping_db_v2 import TestMappingDBV2


class DeltaRunner:
    """Select and run tests based on code coverage and git changes."""
    
    def __init__(self, repo_root: Path, mapping_db=None, verbose: bool = False):
        """
        Initialize runner.
        
        Args:
            repo_root: Repository root directory
            mapping_db: Mapping database object (TestMappingDBV2 or CloudMappingDB)
            verbose: Enable verbose output
        """
        self.repo_root = Path(repo_root).resolve()
        self.git_parser = GitDiffParser(self.repo_root)
        self.mapping_db = mapping_db
        self.verbose = verbose
        self.explanation = {}
        
    def find_affected_tests(self, base_branch: str = "master") -> Set[str]:
        """
        Find all tests affected by changes since base_branch.
        
        Returns:
            Set of test identifiers (pytest format)
        """
        # Get git diff and merge base
        diff_output, merge_base = self.git_parser.get_diff(base_branch)
        
        changes = self.git_parser.parse_diff(diff_output)
        python_changes = self.git_parser.filter_python_files(changes)
        
        if self.verbose:
            print(f"Found {len(python_changes)} changed Python files", file=sys.stderr)
        
        # Get changed test files (always run these)
        changed_test_files = self.git_parser.get_changed_test_files(python_changes)
        
        if self.verbose and changed_test_files:
            print(f"Changed test files: {len(changed_test_files)}", file=sys.stderr)
            for test_file in sorted(changed_test_files):
                print(f"  - {test_file}", file=sys.stderr)
        
        # Find tests that cover changed lines
        affected_tests = set()
        
        if self.mapping_db:
            if not python_changes:
                if self.verbose:
                    print("No Python source changes detected. Skipping mapping query.", file=sys.stderr)
            elif hasattr(self.mapping_db, 'find_tests_for_changes'):
                # CloudMappingDB or optimized interface
                # Format changes for the API: [{"file": "path", "lines": [1, 2]}, ...]
                formatted_changes = [
                    {"file": path, "lines": list(change.get_all_changed_lines())}
                    for path, change in python_changes.items()
                    if not change.is_new and change.get_all_changed_lines()
                ]
                
                if formatted_changes:
                    # Pass base_branch as the target branch for mapping lookup and merge_base as the exact commit
                    affected_tests, unmapped = self.mapping_db.find_tests_for_changes(
                        formatted_changes, 
                        branch=base_branch,
                        commit_sha=merge_base
                    )
                    if getattr(self.mapping_db, 'explanation', None):
                        for test_name, entries in self.mapping_db.explanation.items():
                            if test_name not in self.explanation:
                                self.explanation[test_name] = []
                            self.explanation[test_name].extend(entries)
                    else:
                        for test_name in affected_tests:
                            if test_name not in self.explanation:
                                self.explanation[test_name] = []
                            self.explanation[test_name].append({
                                "reason": "Matched by Delta Cloud mapping for changes"
                            })
                    if self.verbose and unmapped:
                        print(f"{len(unmapped)} files have no coverage mapping", file=sys.stderr)
                elif self.verbose:
                    print("Changes contain no covered lines. Skipping mapping query.", file=sys.stderr)
            else:
                # Local TestMappingDBV2
                from .range_set import RangeSet
                for file_path, change in python_changes.items():
                    # Skip test files (we already have them)
                    if file_path in changed_test_files:
                        continue
                    
                    if change.is_new:
                        continue
                    
                    changed_lines = change.get_all_changed_lines()
                    if not changed_lines:
                        continue
                    
                    # Find tests covering these lines and explain them
                    cursor = self.mapping_db.conn.cursor()
                    cursor.execute("""
                        SELECT test_name, ranges
                        FROM test_coverage_ranges
                        WHERE file_path = ?
                    """, (str(file_path),))
                    
                    file_affected_tests_count = 0
                    for row in cursor.fetchall():
                        ranges = RangeSet.from_compact_string(row['ranges'])
                        intersecting = {line for line in changed_lines if ranges.contains(line)}
                        if intersecting:
                            test_name = row['test_name']
                            affected_tests.add(test_name)
                            file_affected_tests_count += 1
                            if test_name not in self.explanation:
                                self.explanation[test_name] = []
                            self.explanation[test_name].append({
                                "file": file_path,
                                "lines": sorted(list(intersecting))
                            })
                    
                    if self.verbose and file_affected_tests_count > 0:
                        print(f"      {file_path}: {file_affected_tests_count} tests affected", file=sys.stderr)
        else:
            print("No mapping database found. Only running changed test files.", file=sys.stderr)
            print("   Run 'delta push' or 'delta build-mapping' to create one.", file=sys.stderr)
        
        # Always include changed test files
        for test_file in changed_test_files:
            affected_tests.add(test_file)
            if test_file not in self.explanation:
                self.explanation[test_file] = []
            self.explanation[test_file].append({
                "reason": "Directly changed test file"
            })
        
        return affected_tests
    
    def run_tests(
        self, 
        test_selection: Set[str], 
        dry_run: bool = False, 
        verbose: bool = False,
        pytest_args: list = None
    ) -> int:
        """
        Run selected tests with pytest.
        
        Args:
            test_selection: Set of test identifiers
            dry_run: If True, only print tests without running
            verbose: Show detailed output
            pytest_args: Additional pytest arguments
            
        Returns:
            Exit code from pytest (0 = success)
        """
        if not test_selection:
            if dry_run:
                print("Would run 0 test(s)")
                return 0
            print("No tests to run!")
            return 0

        test_list = sorted(test_selection)
        chunk_size = 1000
        has_xdist = pytest_args and any(
            arg == '-n' or arg.startswith('-n=') or arg == '--numprocesses' or arg.startswith('--numprocesses=')
            for arg in pytest_args
        )
        needs_chunking = len(test_list) > chunk_size and not has_xdist

        # Base pytest command
        base_cmd = [sys.executable, "-m", "pytest", "-p", "delta.pytest_plugin"]
        base_cmd.extend(["--cov", "--cov-context=test", "--cov-append", "--cov-report="])
        if verbose:
            base_cmd.append("-v")
        if pytest_args:
            base_cmd.extend(pytest_args)

        if dry_run:
            print(f"Would run {len(test_selection)} test(s):")
            for test in test_list[:10]:
                print(f"  {test}")
            if len(test_list) > 10:
                print(f"  ... and {len(test_list) - 10} more")
            print(f"\nBase Command: {' '.join(base_cmd)}")
            return 0

        if needs_chunking:
            print(f"Running {len(test_selection)} selected test(s) in chunks of {chunk_size}...")
            total_chunks = (len(test_list) + chunk_size - 1) // chunk_size
            final_returncode = 0
            fail_fast = pytest_args and any(arg == '-x' or arg.startswith('--maxfail') for arg in pytest_args)
            
            import json
            delta_dir = self.repo_root / ".delta"
            delta_dir.mkdir(parents=True, exist_ok=True)
            
            for i in range(0, len(test_list), chunk_size):
                chunk = test_list[i:i + chunk_size]
                chunk_num = (i // chunk_size) + 1
                print(f"\nRunning chunk {chunk_num}/{total_chunks} ({len(chunk)} tests)...", flush=True)
                
                chunk_file = delta_dir / f"chunk_{chunk_num}.json"
                with open(chunk_file, "w") as f:
                    json.dump(chunk, f)
                
                cmd = base_cmd + ["--delta-select-file", str(chunk_file)]
                result = subprocess.run(cmd, cwd=self.repo_root)

                # Auto-update the local database with the new coverage after each chunk
                if self.mapping_db and hasattr(self.mapping_db, 'db_path'):
                    print("\nUpdating mapping database with chunk coverage...")
                    from .update_mapping import update_mapping
                    update_mapping(self.repo_root, mapping_db=self.mapping_db.db_path, verbose=verbose, incremental=True)

                if result.returncode != 0:
                    ret_code = 0 if result.returncode == 5 else result.returncode
                    if final_returncode == 0 and ret_code != 0:
                        final_returncode = ret_code
                    if fail_fast and ret_code != 0:
                        print(f"\nFail-fast enabled (-x / --maxfail). Aborting remaining {total_chunks - chunk_num} chunk(s).", file=sys.stderr)
                        break
            return final_returncode
        else:
            print(f"Running {len(test_selection)} selected test(s)...")
            import json
            delta_dir = self.repo_root / ".delta"
            delta_dir.mkdir(parents=True, exist_ok=True)
            select_file = delta_dir / "xdist_select.json"
            with open(select_file, "w") as f:
                json.dump(test_list, f)
            cmd = base_cmd + ["--delta-select-file", str(select_file)]
            result = subprocess.run(cmd, cwd=self.repo_root)
            # Pytest exit code 5 means no tests were collected.
            # We map this to 0 (success) because it is a valid outcome when filtering.
            if result.returncode == 5:
                print("No tests were collected by pytest (all deselected or skipped).")
                return 0
            return result.returncode


def cmd_run(args):
    """Run affected tests."""
    repo_root = args.repo_root.resolve()
    if not repo_root.exists():
        print(f"Error: Repository not found: {repo_root}", file=sys.stderr)
        sys.exit(1)
    
    if getattr(args, 'subprocess', False):
        import os
        coveragerc_path = repo_root / ".coveragerc"
        os.environ["COVERAGE_PROCESS_START"] = str(coveragerc_path)
    
    # ── Mapping source detection ───────────────────────────────────────────
    mapping_db_obj = None
    
    # 1. Try Cloud Mode
    cfg = Config.load()
    use_cloud = cfg.is_cloud_enabled and bool(cfg.cloud.repo_id) and not getattr(args, 'local', False)
    
    if use_cloud:
        if args.verbose:
            print(f"Mapping Service: Delta Cloud (repo {cfg.cloud.repo_id[:8]}...)", file=sys.stderr)
        mapping_db_obj = CloudMappingDB(cfg.cloud)
    else:
        mapping_db = args.mapping_db if hasattr(args, 'mapping_db') and args.mapping_db else None
        if not mapping_db:
            # Auto-detect base_branch BEFORE constructing DB path
            base_branch = getattr(args, 'base_branch', 'master')
            if base_branch == "master":
                if cfg.is_cloud_enabled and getattr(cfg.cloud, "branch", None):
                    base_branch = cfg.cloud.branch
                else:
                    try:
                        res = subprocess.run(
                            ["git", "rev-parse", "--verify", "--quiet", "refs/heads/master"],
                            cwd=repo_root, capture_output=True, text=True
                        )
                        if res.returncode != 0:
                            res = subprocess.run(
                                ["git", "rev-parse", "--verify", "--quiet", "refs/heads/main"],
                                cwd=repo_root, capture_output=True, text=True
                            )
                            if res.returncode == 0:
                                base_branch = "main"
                    except Exception:
                        pass
                # Update args.base_branch so auto-detected value is used downstream
                args.base_branch = base_branch
            elif base_branch == "HEAD":
                try:
                    base_branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_root, capture_output=True, text=True, check=True).stdout.strip()
                except Exception:
                    base_branch = "master"
            try:
                current_branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_root, capture_output=True, text=True, check=True).stdout.strip()
                if current_branch == "HEAD":
                    current_branch = "master"
            except Exception:
                current_branch = "master"
            safe_branch = current_branch.replace('/', '_')
            mapping_db = repo_root / ".delta" / f"test_mapping_{safe_branch}.db"
            fallback_db = repo_root / ".delta" / "test_mapping.db"
            legacy_db = repo_root / ".test_mapping.db"
            
            if not mapping_db.exists():
                if fallback_db.exists():
                    mapping_db = fallback_db
                elif legacy_db.exists():
                    fallback_db.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        legacy_db.rename(fallback_db)
                        mapping_db = fallback_db
                    except Exception:
                        mapping_db = legacy_db
            
        if not mapping_db.exists():
            print("No local mapping DB found. Initializing build...", file=sys.stderr)
            from .build_mapping_iterative import build_mapping_iteratively
            
            # Use test_dir from args, config, or default "tests"
            initial_test_dir = getattr(args, 'test_dir', None)
            if not initial_test_dir:
                initial_test_dir = getattr(cfg.cloud, "test_dir", None)
            if not initial_test_dir:
                initial_test_dir = "tests"
                
            exit_code = build_mapping_iteratively(
                repo_root,
                mapping_db,
                test_dir=initial_test_dir,
                verbose=args.verbose,
                no_remote=getattr(args, 'local', False)
            )
            sys.exit(exit_code)
            
        if args.verbose:
            print(f"Local mode: {mapping_db}", file=sys.stderr)
        mapping_db_obj = TestMappingDBV2(mapping_db)
        mapping_db_obj.connect()

    # Initialize runner
    runner = DeltaRunner(
        repo_root, 
        mapping_db=mapping_db_obj,
        verbose=args.verbose
    )
    
    try:
        base_branch = args.base_branch
        # Note: branch auto-detection (master->main) was already done earlier
        # during DB path construction

        # Find affected tests
        affected_tests = runner.find_affected_tests(
            base_branch
        )
        
        # Always run previously failed tests
        lastfailed_file = repo_root / ".pytest_cache" / "v" / "cache" / "lastfailed"
        if lastfailed_file.exists():
            try:
                import json as _json
                with open(lastfailed_file) as _f:
                    lastfailed_tests = _json.load(_f)
                    failed_count = 0
                    for test in lastfailed_tests.keys():
                        # lastfailed sometimes contains file paths if the whole file failed collection
                        affected_tests.add(test)
                        if test not in runner.explanation:
                            runner.explanation[test] = []
                        runner.explanation[test].append({
                            "reason": "Failed in previous run"
                        })
                        failed_count += 1
                    if failed_count and args.verbose:
                        print(f"Added {failed_count} previously failed test(s)", file=sys.stderr)
            except Exception:
                pass
        
        # Determine test_dir
        test_dir = args.test_dir
        if not test_dir and cfg.is_cloud_enabled and getattr(cfg.cloud, "test_dir", None):
            test_dir = cfg.cloud.test_dir
        if not test_dir:
            test_dir = "tests"

        # Unmapped tests discovery
        if mapping_db_obj:
            from .pre_commit_hook import find_unmapped_tests
            unmapped = find_unmapped_tests(
                repo_root,
                mapping_db_obj,
                test_dir=test_dir,
                verbose=args.verbose
            )
            if unmapped:
                if args.verbose:
                    print(f"Found {len(unmapped)} unmapped tests", file=sys.stderr)
                affected_tests.update(unmapped)
                for test in unmapped:
                    if test not in runner.explanation:
                        runner.explanation[test] = []
                    runner.explanation[test].append({
                        "reason": "Unmapped test (runs to build coverage)"
                    })
        
        # Load known-skipped tests from the file written by the pytest plugin
        skipped_tests: set = set()
        skipped_file = repo_root / ".delta" / "skipped_tests.json"
        if skipped_file.exists():
            try:
                import json as _json
                with open(skipped_file) as _f:
                    skipped_tests = set(_json.load(_f))
            except Exception:
                pass

        # Filter skipped tests out of affected set before explain and before run
        skipped_count = 0
        if skipped_tests:
            to_bypass = affected_tests & skipped_tests
            if to_bypass:
                skipped_count = len(to_bypass)
                affected_tests -= to_bypass
                # Also remove from explanation so they don't appear in --explain
                for t in to_bypass:
                    runner.explanation.pop(t, None)
                print(f"Skipping {skipped_count} known-skipped test(s)")

        # Filter out tests that do not exist on disk (e.g. deleted or moved test files)
        existing_affected_tests = set()
        for test in affected_tests:
            file_part = test.split("::")[0]
            if (repo_root / file_part).exists():
                existing_affected_tests.add(test)
            else:
                runner.explanation.pop(test, None)
                if args.verbose:
                    print(f"Filtering out non-existent test file: {file_part} (from {test})", file=sys.stderr)
        affected_tests = existing_affected_tests

        # Print explanation if requested
        if getattr(args, 'explain', False):
            print("\n================================================================================")
            print("Delta Test Selection Explanation")
            if skipped_count:
                print(f"({skipped_count} skipped test(s) excluded from this report)")
            print("================================================================================")

            for test_name, entries in sorted(runner.explanation.items()):
                print(f"\nTest: {test_name}")
                for entry in entries:
                    if "reason" in entry:
                        print(f"  - Reason: {entry['reason']}")
                    else:
                        lines_str = ", ".join(map(str, entry["lines"]))
                        print(f"  - Affected by: {entry['file']} (lines: {lines_str})")
            print("\n================================================================================\n")
        
        # Check minimum threshold
        if args.min_tests > 0 and len(affected_tests) < args.min_tests:
            print(
                f"Only {len(affected_tests)} test(s) found, below minimum {args.min_tests}",
                file=sys.stderr
            )
            print("Exiting with error (run full test suite instead)", file=sys.stderr)
            sys.exit(1)
        
        # Remove '--' separator if present in unknown_args
        pytest_extra_args = getattr(args, 'unknown_args', [])
        if pytest_extra_args and pytest_extra_args[0] == '--':
            pytest_extra_args = pytest_extra_args[1:]
        
        # Run tests
        exit_code = runner.run_tests(
            affected_tests, 
            dry_run=args.dry_run,
            verbose=args.verbose,
            pytest_args=pytest_extra_args
        )
        
        # Auto-update the local database with the new coverage
        if not use_cloud and not args.dry_run:
            print("\nUpdating mapping database with new coverage...")
            from .update_mapping import update_mapping
            update_mapping(repo_root, mapping_db=mapping_db, verbose=args.verbose)
            # cleanup_coverage_temps is called inside update_mapping()
        elif not args.dry_run:
            # Cloud mode: mapping update happens server-side, but we still need
            # to clean up the local parallel-worker .coverage.* temp files.
            from .update_mapping import cleanup_coverage_temps
            cleanup_coverage_temps(repo_root, verbose=args.verbose)

        sys.exit(exit_code)
        
    except KeyboardInterrupt:
        print("\nInterrupted by user", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        if args.verbose:
            import traceback
            traceback.print_exc()
        sys.exit(1)
    finally:
        if mapping_db_obj and hasattr(mapping_db_obj, 'close'):
            mapping_db_obj.close()


def cmd_update_mapping(args):
    """Update test mapping database from coverage."""
    from .update_mapping import update_mapping
    sys.exit(update_mapping(
        args.repo_root,
        args.coverage_file,
        args.mapping_db,
        args.verbose
    ))


def cmd_status(args):
    """Show Delta mapping database status and statistics."""
    repo_root = args.repo_root.resolve()
    if not repo_root.exists():
        print(f"Error: Repository not found: {repo_root}", file=sys.stderr)
        sys.exit(1)

    print("=== Delta Status ===")
    print(f"Repository root: {repo_root}")

    # Load configuration
    cfg = Config.load()
    cloud_enabled = cfg.is_cloud_enabled
    
    print("\n--- Cloud Settings ---")
    if cloud_enabled:
        print("Integration:  Enabled")
        print(f"API URL:      {cfg.cloud.api_url}")
        print(f"Repository ID: {cfg.cloud.repo_id}")
        print(f"Default branch: {cfg.cloud.branch}")
    else:
        print("Integration:  Disabled")

    # Check local database
    mapping_db = getattr(args, 'mapping_db', None)
    if not mapping_db:
        mapping_db = repo_root / ".delta" / "test_mapping.db"
        legacy_db = repo_root / ".test_mapping.db"
        if not mapping_db.exists() and legacy_db.exists():
            mapping_db = legacy_db

    print("\n--- Local Database ---")
    if mapping_db.exists():
        print(f"Database file: {mapping_db}")
        try:
            with TestMappingDBV2(mapping_db) as db:
                stats = db.get_stats()
                metadata = db.get_metadata()
                
                print(f"Total mapped tests:  {stats.get('total_tests', 0)}")
                print(f"Total mapped files:  {stats.get('total_files', 0)}")
                print(f"Total mappings:      {stats.get('total_mappings', 0)}")
                print(f"Last updated:        {metadata.get('last_import', 'N/A')}")
                if metadata.get('source_coverage_file'):
                    print(f"Source coverage:     {metadata.get('source_coverage_file')}")
        except Exception as e:
            print(f"Error reading local database: {e}", file=sys.stderr)
    else:
        print("Database status: Not initialized")
        print("   Generate database using: delta build-mapping")

    # Check remote/cloud database if enabled
    if cloud_enabled and cfg.cloud.repo_id:
        print("\n--- Remote Database (Delta Cloud) ---")
        try:
            remote_db = CloudMappingDB(cfg.cloud)
            print("Connecting to Delta Cloud API...")
            remote_stats = remote_db.get_stats()
            print(f"Total mapped tests:  {remote_stats.get('total_tests', '?')}")
            print(f"Total mapped files:  {remote_stats.get('files_covered', '?')}")
        except Exception as e:
            print(f"Error connecting to cloud: {e}", file=sys.stderr)
            
    print("\n====================")
    sys.exit(0)


def cmd_build_mapping(args):
    """Build test mapping database iteratively (resumable)."""
    from .build_mapping_iterative import build_mapping_iteratively
    
    repo_root = args.repo_root.resolve()
    if args.mapping_db:
        mapping_db = args.mapping_db
    else:
        try:
            current_branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_root, capture_output=True, text=True, check=True).stdout.strip()
            if current_branch == "HEAD":
                current_branch = "master"
        except Exception:
            current_branch = "master"
        safe_branch = current_branch.replace('/', '_')
        mapping_db = repo_root / ".delta" / f"test_mapping_{safe_branch}.db"
        fallback_db = repo_root / ".delta" / "test_mapping.db"
        legacy_db = repo_root / ".test_mapping.db"
        
        # We don't read fallback here because we are building a NEW one, but we 
        # ensure its parent exists.
        mapping_db.parent.mkdir(parents=True, exist_ok=True)
        if not mapping_db.exists() and legacy_db.exists():
            try:
                legacy_db.rename(fallback_db)
            except Exception:
                pass
    
    # Load config to get default test_dir if it was saved during track
    from .config import Config
    cfg = Config.load()
    default_test_dir = "."
    if cfg.cloud and getattr(cfg.cloud, "test_dir", None):
        default_test_dir = cfg.cloud.test_dir

    # Use args.test_dir only if explicitly provided on CLI; otherwise use config value
    final_test_dir = args.test_dir if args.test_dir else default_test_dir

    pytest_args = getattr(args, 'unknown_args', [])
    if pytest_args and pytest_args[0] == '--':
        pytest_args = pytest_args[1:]

    sys.exit(build_mapping_iteratively(
        repo_root,
        mapping_db,
        test_dir=final_test_dir,
        verbose=args.verbose,
        pytest_args=pytest_args,
        no_remote=getattr(args, 'local', False),
        subprocess_mode=getattr(args, 'subprocess', False)
    ))


def cmd_login(args):
    """Authenticate with Delta and save API key to ~/.delta/config.toml."""
    from .config import Config, CloudConfig, CONFIG_FILE

    api_url = args.api_url.rstrip("/")

    print(f"\nDelta Login ({api_url})")
    print("────────────────────────────────────────")

    # Offer two paths: API key directly, or email/password
    print("\nOptions:")
    print("  1) Paste an existing API key (from https://deltatest.dev/dashboard/keys)")
    print("  2) Login with email + password to generate a new key")
    choice = input("\nChoice [1/2]: ").strip()

    try:
        import requests
    except ImportError:
        print("'requests' is required: pip install requests")
        sys.exit(1)

    if choice == "1":
        api_key = getpass.getpass("Paste API key (pt_live_...): ").strip()
        if not api_key.startswith("pt_live_"):
            print("Invalid key format — expected 'pt_live_...'")
            sys.exit(1)
    else:
        email = input("Email: ").strip()
        password = getpass.getpass("Password: ")

        # Login to get JWT, then create a new API key
        resp = requests.post(
            f"{api_url}/api/v1/auth/login",
            json={"email": email, "password": password},
            timeout=15,
        )
        if resp.status_code == 401:
            print("Invalid email or password")
            sys.exit(1)
        resp.raise_for_status()
        jwt_token = resp.json()["access_token"]

        # Create a new API key scoped to query + push
        key_resp = requests.post(
            f"{api_url}/api/v1/auth/keys",
            json={"name": "cli-key", "scopes": ["query", "push"]},
            headers={"Authorization": f"Bearer {jwt_token}"},
            timeout=15,
        )
        key_resp.raise_for_status()
        api_key = key_resp.json()["key"]
        print(f"   New API key created (ID: {key_resp.json()['id']})")

    # Save to config
    cfg = Config.load()
    cfg.cloud = CloudConfig(api_key=api_key, api_url=api_url)
    cfg.save()

    print(f"\nLogged in! Config saved to {CONFIG_FILE}")
    print(f"   Next step: delta track")


def cmd_register(args):
    """Register a new account with Delta."""
    api_url = args.api_url.rstrip("/")
    print(f"\nDelta Registration ({api_url})")
    print("────────────────────────────────────────")
    
    email = input("Email: ").strip()
    password = getpass.getpass("Password: ")
    org_name = input("Organization Name: ").strip()
    
    try:
        import requests
        resp = requests.post(
            f"{api_url}/api/v1/auth/register",
            json={"email": email, "password": password, "org_name": org_name},
            timeout=15,
        )
        if resp.status_code == 400:
            print(f"{resp.json().get('detail', 'Registration failed')}")
            sys.exit(1)
        resp.raise_for_status()
        print("Account created successfully!")
        print(f"   Organization '{org_name}' registered on the Free plan.")
        print("   You can now log in using 'delta login'")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


def cmd_track(args):
    """Register a repository with Delta and save repo_id to config."""
    import os
    import sys
    from .config import Config

    repo_name = args.name
    if not repo_name:
        repo_name = os.path.basename(os.getcwd())

    branch = args.branch
    if not branch:
        import subprocess
        try:
            default_branch = subprocess.run(
                ["git", "branch", "--show-current"],
                capture_output=True,
                text=True,
                check=True
            ).stdout.strip()
            if not default_branch:
                default_branch = "main"
        except Exception:
            default_branch = "main"

        try:
            branch = input(f"Target branch to track [{default_branch}]: ").strip()
            if not branch:
                branch = default_branch
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled")
            sys.exit(1)
            
    try:
        test_dir = input("Test directory [.]: ").strip()
        if not test_dir:
            test_dir = "."
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled")
        sys.exit(1)

    cfg = Config.load()
    if not cfg.is_cloud_enabled:
        print("Not logged in. Run: delta login")
        sys.exit(1)

    try:
        import requests
    except ImportError:
        print("'requests' is required: pip install requests")
        sys.exit(1)

    api_url = cfg.cloud.api_url
    headers = {
        "Authorization": f"Bearer {cfg.cloud.api_key}",
        "Content-Type": "application/json",
    }

    print(f"\nRegistering repo '{repo_name}' with Delta...")

    resp = requests.post(
        f"{api_url}/api/v1/repos",
        json={
            "name": repo_name,
            "remote_url": args.remote_url,
            "default_branch": branch,
        },
        headers=headers,
        timeout=15,
    )

    if resp.status_code == 400:
        detail = resp.json().get("detail", "")
        print(f"{detail}")
        sys.exit(1)
    
    if resp.status_code == 402:
        detail = resp.json().get("detail", "")
        print(f"\n{detail}")
        sys.exit(1)
        
    resp.raise_for_status()

    repo = resp.json()
    repo_id = repo["id"]

    # Save repo_id, branch, and test_dir to config
    cfg.cloud.repo_id = repo_id
    cfg.cloud.branch = branch
    cfg.cloud.test_dir = test_dir
    cfg.save()
    
    print(f"Success! Repository mapped to {branch} branch")
    print(f"   Name:    {repo['name']}")
    print(f"   ID:      {repo_id}")
    print(f"   Branch:  {branch}")
    print(f"   Config:  ~/.delta/config.toml")
    print(f"\n   Your pre-commit hook will now use the Delta cloud API automatically.")


def cmd_push(args):
    """Sync local coverage mappings to Delta cloud."""
    from .cloud_mapping_db import CloudMappingDB
    from .config import Config

    repo_root = Path.cwd()
    config = Config.load()
    
    if not config.is_cloud_enabled or not config.cloud.repo_id:
        print("Repository not registered. Run 'delta track' first.")
        sys.exit(1)

    coverage_file = repo_root / ".coverage"
    if not coverage_file.exists() and (repo_root / "coverage" / ".coverage").exists():
        coverage_file = repo_root / "coverage" / ".coverage"
    if not coverage_file.exists():
        print(f"Coverage file not found at {coverage_file}")
        print("Run your tests first (e.g. 'delta build-mapping')")
        sys.exit(1)

    print(f"Delta Cloud Sync (repo: {config.cloud.repo_id[:8]}...)")
    db = CloudMappingDB(config.cloud)
    
    success = db.push_coverage(coverage_file, verbose=args.verbose)
    
    if success:
        print("Success!")
    else:
        print("Push failed.")
        sys.exit(1)


def cmd_install(args):
    """Install Delta git hook(s)."""
    import subprocess
    import shutil

    # Ensure we are inside a git repository
    repo_root = getattr(args, 'repo_root', Path.cwd()).resolve()
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True
        )
        git_root = Path(res.stdout.strip()).resolve()
    except subprocess.CalledProcessError:
        print("Error: Not in a git repository", file=sys.stderr)
        sys.exit(1)

    # Find the hooks directory
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--git-path", "hooks"],
            cwd=git_root,
            capture_output=True,
            text=True,
            check=True
        )
        hooks_dir = Path(res.stdout.strip())
        if not hooks_dir.is_absolute():
            hooks_dir = (git_root / hooks_dir).resolve()
    except subprocess.CalledProcessError:
        hooks_dir = git_root / ".git" / "hooks"

    hooks_dir.mkdir(parents=True, exist_ok=True)

    base_branch = getattr(args, 'base_branch', 'master')

    # If the default 'master' was used, check if it actually exists in this repository.
    # If not, try to auto-detect the current branch name (e.g. 'main').
    if base_branch == "master":
        try:
            # Check if master branch exists in this repo (local or remote)
            subprocess.run(
                ["git", "show-ref", "--verify", "--quiet", "refs/heads/master"],
                cwd=git_root,
                check=True
            )
        except subprocess.CalledProcessError:
            # master branch does not exist, try to detect current active branch
            try:
                res = subprocess.run(
                    ["git", "branch", "--show-current"],
                    cwd=git_root,
                    capture_output=True,
                    text=True,
                    check=True
                )
                curr = res.stdout.strip()
                if curr:
                    base_branch = curr
                else:
                    base_branch = "main"
            except Exception:
                base_branch = "main"

    # Install pre-commit hook
    pre_commit_path = hooks_dir / "pre-commit"
    if pre_commit_path.exists():
        backup_path = pre_commit_path.with_suffix(".backup")
        print(f"   Backing up existing pre-commit hook to {backup_path.name}...")
        try:
            shutil.move(str(pre_commit_path), str(backup_path))
        except Exception as e:
            print(f"Could not backup existing pre-commit hook: {e}", file=sys.stderr)

    pre_commit_content = f"""#!/bin/bash
# Delta Pre-Commit Hook
# Runs only tests affected by code changes

set -e

REPO_ROOT="$(git rev-parse --show-toplevel)"

# Find the correct Python interpreter
PYTHON_CMD="python3"
if [ -f "$REPO_ROOT/.venv/bin/python3" ]; then
    PYTHON_CMD="$REPO_ROOT/.venv/bin/python3"
elif [ -f "$REPO_ROOT/venv/bin/python3" ]; then
    PYTHON_CMD="$REPO_ROOT/venv/bin/python3"
fi

$PYTHON_CMD -m delta.pre_commit_hook \\
    --repo-root "$REPO_ROOT" \\
    --base-branch {base_branch} \\
    --verbose

exit $?
"""

    try:
        pre_commit_path.write_text(pre_commit_content)
        pre_commit_path.chmod(pre_commit_path.stat().st_mode | 0o111)
        print("   Pre-commit hook installed successfully.")
    except Exception as e:
        print(f"Error writing pre-commit hook: {e}", file=sys.stderr)
        sys.exit(1)

    # Clean up deprecated Delta post-commit hook if it exists
    post_commit_path = hooks_dir / "post-commit"
    if post_commit_path.exists():
        try:
            content = post_commit_path.read_text()
            if any(sig in content for sig in ["Delta Post-Commit Hook", "pintest.post_commit_hook", "delta.post_commit_hook"]):
                post_commit_path.unlink()
                print("   Removed deprecated Delta post-commit hook (auto-push is disabled).")
                # Restore backup if it exists
                backup_path = post_commit_path.with_suffix(".backup")
                if backup_path.exists():
                    try:
                        shutil.move(str(backup_path), str(post_commit_path))
                        print("   Restored previous post-commit hook backup.")
                    except Exception as e:
                        print(f"Could not restore backup of post-commit hook: {e}", file=sys.stderr)
        except Exception as e:
            print(f"Could not clean up post-commit hook: {e}", file=sys.stderr)

    # Update .gitignore
    gitignore_path = git_root / ".gitignore"
    gitignore_entries = [".test_mapping.db", ".delta/"]
    
    # Read existing gitignore to prevent duplicate entries
    existing_content = ""
    if gitignore_path.exists():
        try:
            existing_content = gitignore_path.read_text()
        except Exception:
            pass

    to_append = []
    for entry in gitignore_entries:
        if entry not in existing_content:
            to_append.append(entry)

    if to_append:
        try:
            with open(gitignore_path, "a") as f:
                if not existing_content.endswith("\n") and existing_content:
                    f.write("\n")
                f.write("\n# Delta\n")
                for entry in to_append:
                    f.write(f"{entry}\n")
            print("   Updated .gitignore with Delta database and config folders.")
        except Exception as e:
            print(f"Could not update .gitignore: {e}", file=sys.stderr)

    print("\nInstallation Complete!")
    print("=========================")
    print("Pre-commit hook will run automatically on every commit.")


def main():
    """CLI entry point."""
    try:
        _main()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        if os.environ.get("DELTA_DEBUG"):
            traceback.print_exc()
        else:
            print(f"\nError: {e}", file=sys.stderr)
            print("   Set DELTA_DEBUG=1 for full traceback.", file=sys.stderr)
        sys.exit(1)


def _main():
    """Internal CLI logic."""
    from . import __version__
    parser = argparse.ArgumentParser(
        description="Delta - Run only tests affected by code changes",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--version", "-V",
        action="version",
        version=f"delta {__version__}"
    )
    
    subparsers = parser.add_subparsers(dest='command', help='Commands')
    
    # Run command (default behavior)
    run_parser = subparsers.add_parser(
        'run',
        help='Run affected tests',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  delta run --repo-root ~/workspace/myproject
  delta run --repo-root ~/workspace/myproject --dry-run
  delta run --repo-root ~/workspace/myproject --base-branch develop
        """
    )
    run_parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Repository root directory (default: current directory)"
    )
    run_parser.add_argument(
        "--local", "--no-remote",
        action="store_true",
        help="Do not connect to remote mapping, only use local mapping database"
    )
    run_parser.add_argument(
        "--base-branch",
        default="master",
        help="Base branch to compare against (default: auto-detected from 'main' or 'master')"
    )
    run_parser.add_argument(
        "--coverage-file",
        type=Path,
        help="Path to coverage database file (default: <repo>/.coverage)"
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print tests without running them"
    )
    run_parser.add_argument(
        "--min-tests",
        type=int,
        default=0,
        help="Minimum number of tests to run (exit with error if below)"
    )
    run_parser.add_argument(
        "--test-dir",
        help="Directory containing tests (default: read from config or tests/)"
    )
    run_parser.add_argument(
        "--subprocess",
        action="store_true",
        help="Enable subprocess coverage tracking"
    )
    run_parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Show detailed output"
    )
    run_parser.add_argument(
        "--explain",
        action="store_true",
        help="Show exactly which tests are affected by which files and lines"
    )
    run_parser.set_defaults(func=cmd_run)
    
    # Update mapping command
    update_parser = subparsers.add_parser(
        'update-mapping',
        help='Update test mapping database from coverage',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  delta update-mapping --repo-root ~/workspace/myproject
  delta update-mapping --repo-root ~/workspace/myproject --verbose
        """
    )
    update_parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Repository root directory (default: current directory)"
    )
    update_parser.add_argument(
        "--coverage-file",
        type=Path,
        help="Path to .coverage file (default: <repo>/.coverage)"
    )
    update_parser.add_argument(
        "--mapping-db",
        type=Path,
        help="Path to mapping database (default: <repo>/.delta/test_mapping.db)"
    )
    update_parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose output"
    )
    update_parser.set_defaults(func=cmd_update_mapping)
    
    # Build mapping iteratively command
    build_parser = subparsers.add_parser(
        'build-mapping',
        help='Build test mapping database iteratively (resumable)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  delta build-mapping --repo-root ~/workspace/myproject
  delta build-mapping --repo-root ~/workspace/myproject --resume
  delta build-mapping --repo-root ~/workspace/myproject --verbose
        """
    )
    build_parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Repository root directory (default: current directory)"
    )
    build_parser.add_argument(
        "--base-branch",
        default=None,
        help="Base branch to target (default: active git branch)"
    )
    build_parser.add_argument(
        "--local", "--no-remote",
        action="store_true",
        help="Do not connect to remote mapping, only build coverage locally"
    )
    build_parser.add_argument(
        "--mapping-db",
        type=Path,
        help="Path to mapping database (default: <repo>/.delta/test_mapping.db)"
    )
    build_parser.add_argument(
        "--test-dir",
        default=None,
        help="Directory containing tests (default: from config or current directory)"
    )
    build_parser.add_argument(
        "--subprocess",
        action="store_true",
        help="Enable subprocess coverage tracking"
    )
    build_parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose output"
    )
    build_parser.set_defaults(func=cmd_build_mapping)

    # Login command
    login_parser = subparsers.add_parser(
        'login',
        help='Authenticate with Delta cloud (saves API key to ~/.delta/config.toml)',
    )
    login_parser.add_argument(
        "--api-url",
        default="https://api.deltatest.dev",
        help="Delta API URL (default: https://api.deltatest.dev)"
    )
    login_parser.set_defaults(func=cmd_login)

    # Register command
    register_parser = subparsers.add_parser(
        'register',
        help='Create a new Delta account',
    )
    register_parser.add_argument(
        "--api-url",
        default="https://api.deltatest.dev",
        help="Delta API URL (default: https://api.deltatest.dev)"
    )
    register_parser.set_defaults(func=cmd_register)

    # Track command
    track_parser = subparsers.add_parser(
        'track',
        help='Register this repository with Delta cloud to track it',
    )
    track_parser.add_argument(
        '--name', required=False,
        help='Repository name (defaults to current directory name)'
    )
    track_parser.add_argument(
        '--branch', '-b', default=None,
        help='Default branch to track (prompts if not provided)'
    )
    track_parser.add_argument(
        '--remote-url',
        default=None,
        help='Remote URL (e.g. https://github.com/org/repo) — optional'
    )
    track_parser.set_defaults(func=cmd_track)
    
    # Push command
    push_parser = subparsers.add_parser(
        'push',
        help='Sync local coverage mappings to Delta cloud',
    )
    push_parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose output"
    )
    push_parser.set_defaults(func=cmd_push)
    
    # Install command
    install_parser = subparsers.add_parser(
        'install',
        help='Install Delta git hook(s)',
    )
    install_parser.add_argument(
        'target',
        nargs='?',
        choices=['pre_commit', 'pre-commit', 'hook'],
        default='pre-commit',
        help='Hook type to install (default: pre-commit)'
    )
    install_parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Repository root directory (default: current directory)"
    )
    install_parser.add_argument(
        "--base-branch",
        default="master",
        help="Base branch to compare against (default: auto-detected from 'main' or 'master')"
    )
    install_parser.set_defaults(func=cmd_install)
    
    # Install-pre-commit command (alias)
    install_pre_commit_parser = subparsers.add_parser(
        'install-pre-commit',
        help='Install Delta git hook(s) (alias of delta install)',
    )
    install_pre_commit_parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Repository root directory (default: current directory)"
    )
    install_pre_commit_parser.add_argument(
        "--base-branch",
        default="master",
        help="Base branch to compare against (default: auto-detected from 'main' or 'master')"
    )
    install_pre_commit_parser.set_defaults(func=cmd_install)
    
    # Status command
    status_parser = subparsers.add_parser(
        'status',
        help='Show Delta mapping database status and statistics',
    )
    status_parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Repository root directory (default: current directory)"
    )
    status_parser.add_argument(
        "--mapping-db",
        type=Path,
        help="Path to mapping database (default: <repo>/.delta/test_mapping.db)"
    )
    status_parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose output"
    )
    status_parser.set_defaults(func=cmd_status)
    
    # Parse arguments
    args, unknown = parser.parse_known_args()
    
    # If no command specified, default to 'run'
    if not args.command:
        # Parse as 'run' command for backward compatibility
        run_args = ['run'] + sys.argv[1:]
        args, unknown = parser.parse_known_args(run_args)
        
    args.unknown_args = unknown
    
    # Execute command
    if hasattr(args, 'func'):
        args.func(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

