#!/usr/bin/env python3
"""
Update test mapping database from coverage data.

Usage:
    python update_mapping.py --repo-root ~/workspace/myproject
"""

import argparse
import sys
from pathlib import Path

# Add delta to path
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from delta.test_mapping_db_v2 import TestMappingDBV2
# from delta.pre_commit_hook import ensure_docker_containers, run_preflight_scripts


def update_mapping(
    repo_root: Path,
    coverage_file: Path = None,
    mapping_db: Path = None,
    verbose: bool = False,
    incremental: bool = False
):
    """
    Update test mapping database from coverage file.
    
    Args:
        repo_root: Repository root directory
        coverage_file: Path to .coverage file
        mapping_db: Path to mapping database file
        verbose: Print verbose output
    """
    repo_root = repo_root.resolve()
    
    # Default paths
    if coverage_file is None:
        coverage_file = repo_root / ".coverage"
        if not coverage_file.exists() and (repo_root / "coverage" / ".coverage").exists():
            coverage_file = repo_root / "coverage" / ".coverage"
    
    if mapping_db is None:
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
    
    if verbose:
        pass
    
    if not coverage_file.exists():
        print(f"Error: Coverage file not found: {coverage_file}", file=sys.stderr)
        print("\nGenerate mapping database first:", file=sys.stderr)
        print(f"  cd {repo_root}", file=sys.stderr)
        print("  delta build-mapping", file=sys.stderr)
        return 1
    
    print(f"Updating test mapping database...")
    print(f"   Coverage file: {coverage_file}")
    print(f"   Mapping DB:    {mapping_db}")
    
    # Initialize database
    with TestMappingDBV2(mapping_db) as db:
        db.initialize_schema()
        
        # Import from coverage
        print("\nImporting coverage data...")
        num_mappings = db.import_from_coverage(coverage_file, incremental=incremental, repo_root=repo_root)
        
        # Get stats
        stats = db.get_stats()
        metadata = db.get_metadata()
        
        print(f"\nTest mapping database updated successfully!")
        print(f"\nStatistics:")
        print(f"   Total tests:    {stats['total_tests']}")
        print(f"   Total files:    {stats['total_files']}")
        print(f"   Total mappings: {stats['total_mappings']}")
        print(f"   Last import:    {metadata.get('last_import', 'N/A')}")
        
        if verbose:
            print(f"\nDatabase size: {mapping_db.stat().st_size / 1024:.1f} KB")

    # Remove leftover parallel-worker coverage files now that data is in the DB
    cleanup_coverage_temps(repo_root, verbose=verbose)

    return 0


def cleanup_coverage_temps(repo_root: Path, verbose: bool = False) -> int:
    """
    Remove temporary .coverage.<hostname>.pid* parallel worker files.

    These are created by pytest-cov when running with --cov-append across
    multiple processes/chunks. Once combined into .coverage they are safe
    to delete.

    Returns:
        Number of files deleted.
    """
    removed = 0
    for p in repo_root.glob(".coverage.*"):
        # Keep .coverage.backup / .coverage.new used by combine_coverage_files,
        # and ignore anything inside .delta/ (already handled separately).
        name = p.name
        if name in (".coverage.backup", ".coverage.new"):
            continue
        try:
            p.unlink()
            removed += 1
            if verbose:
                print(f"   Removed temp coverage file: {name}")
        except Exception as e:
            if verbose:
                print(f"   Could not remove {name}: {e}")
    if removed:
        print(f"Cleaned up {removed} temporary coverage file(s)")
    return removed


def main():
    parser = argparse.ArgumentParser(
        description="Update test mapping database from coverage data"
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Repository root directory (default: current directory)"
    )
    parser.add_argument(
        "--coverage-file",
        type=Path,
        help="Path to .coverage file (default: <repo>/.coverage)"
    )
    parser.add_argument(
        "--mapping-db",
        type=Path,
        help="Path to mapping database (default: <repo>/.delta/test_mapping.db)"
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose output"
    )
    
    args = parser.parse_args()
    
    return update_mapping(
        args.repo_root,
        args.coverage_file,
        args.mapping_db,
        args.verbose
    )


if __name__ == "__main__":
    sys.exit(main())
