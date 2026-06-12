"""Range-based test mapping storage for dramatic size reduction.

Optimized version that stores line ranges instead of individual lines,
reducing 12M entries to ~100K ranges (99%+ reduction).
"""

import sqlite3
from pathlib import Path
from typing import Set, List, Dict, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime

from .range_set import RangeSet


def normalize_test_name(test_name: str) -> str:
    """
    Normalize test name by removing parametrization.
    
    This aggregates parametrized tests into their base test name:
    - test_foo[param1] -> test_foo
    - test_foo[param2] -> test_foo
    
    This prevents:
    1. Storing duplicate entries for each parameter combination
    2. Issues with dynamic parameters that change between runs
    
    Args:
        test_name: Full test name including parametrization
        
    Returns:
        Base test name without parametrization
    """
    # Strip parametrization: test_name[params] -> test_name
    if "[" in test_name:
        return test_name.split("[")[0]
    return test_name



@dataclass
class TestMapping:
    """Represents a test and the files/lines it covers."""
    test_name: str
    file_path: str
    lines: RangeSet


class TestMappingDBV2:
    """SQLite database with range-based storage for test-to-code mappings."""
    
    SCHEMA_VERSION = 2
    
    def __init__(self, db_path: Path):
        """
        Initialize mapping database.
        
        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = Path(db_path)
        self.conn: Optional[sqlite3.Connection] = None
        
    def __enter__(self):
        """Context manager entry."""
        self.connect()
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()
        
    def connect(self):
        """Open database connection."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        
        # Performance optimizations
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA cache_size=-64000")  # 64MB cache
        self.conn.execute("PRAGMA temp_store=MEMORY")
        self.conn.execute("PRAGMA mmap_size=268435456")  # 256MB memory-mapped I/O
        
        # Auto-initialize schema if not present
        if not self.is_initialized():
            self.initialize_schema()
        
    def close(self):
        """Close database connection."""
        if self.conn:
            self.conn.close()
            self.conn = None
    
    def initialize_schema(self):
        """Create database tables with range-based storage."""
        cursor = self.conn.cursor()
        
        # Metadata table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        
        # Range-based test coverage table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS test_coverage_ranges (
                test_name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                ranges TEXT NOT NULL,
                PRIMARY KEY (test_name, file_path)
            )
        """)
        
        # Indexes for fast lookups
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_ranges_file_path 
            ON test_coverage_ranges(file_path)
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_ranges_test_name 
            ON test_coverage_ranges(test_name)
        """)
        
        # Store schema version
        cursor.execute("""
            INSERT OR REPLACE INTO metadata (key, value)
            VALUES ('schema_version', ?)
        """, (str(self.SCHEMA_VERSION),))
        
        # Store creation timestamp
        cursor.execute("""
            INSERT OR IGNORE INTO metadata (key, value)
            VALUES ('created_at', ?)
        """, (datetime.utcnow().isoformat(),))
        
        self.conn.commit()
    
    def is_initialized(self) -> bool:
        """Check if database schema is initialized."""
        try:
            cursor = self.conn.cursor()
            cursor.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='table' AND name='test_coverage_ranges'
            """)
            return cursor.fetchone() is not None
        except sqlite3.Error:
            return False
    
    def import_from_coverage(self, coverage_file: Path, incremental: bool = False):
        """
        Import test mappings from pytest .coverage file.
        
        Args:
            coverage_file: Path to .coverage SQLite file
            incremental: If True, merge with existing mappings
        """
        from .coverage_mapper import CoverageMapper
        
        mapper = CoverageMapper(coverage_file)
        mapper.load_coverage()
        
        cursor = self.conn.cursor()
        
        # Build range-compressed mappings
        test_file_ranges: Dict[Tuple[str, str], RangeSet] = {}
        
        for file_path, coverage_data in mapper.coverage_data.items():
            for line_num, test_contexts in coverage_data.test_contexts.items():
                for test_name in test_contexts:
                    # Normalize test name to aggregate parametrized tests
                    normalized_test = normalize_test_name(test_name)
                    
                    key = (normalized_test, file_path)
                    if key not in test_file_ranges:
                        test_file_ranges[key] = RangeSet()
                    test_file_ranges[key].add_range(line_num, line_num)
        
        if incremental:
            # Merge with existing ranges in bulk to avoid performance bottleneck
            cursor.execute("SELECT test_name, file_path, ranges FROM test_coverage_ranges")
            existing_mappings = {
                (row['test_name'], row['file_path']): row['ranges']
                for row in cursor.fetchall()
            }
            
            records = []
            for (test_name, file_path), new_ranges in test_file_ranges.items():
                key = (test_name, file_path)
                if key in existing_mappings:
                    existing = RangeSet.from_compact_string(existing_mappings[key])
                    merged = existing.union(new_ranges)
                    records.append((test_name, file_path, merged.to_compact_string()))
                else:
                    records.append((test_name, file_path, new_ranges.to_compact_string()))
            
            cursor.executemany("""
                INSERT OR REPLACE INTO test_coverage_ranges (test_name, file_path, ranges)
                VALUES (?, ?, ?)
            """, records)
        else:
            # Replace all mappings
            cursor.execute("DELETE FROM test_coverage_ranges")
            
            records = [
                (test_name, file_path, ranges.to_compact_string())
                for (test_name, file_path), ranges in test_file_ranges.items()
            ]
            
            cursor.executemany("""
                INSERT INTO test_coverage_ranges (test_name, file_path, ranges)
                VALUES (?, ?, ?)
            """, records)
        
        # Update metadata
        cursor.execute("""
            INSERT OR REPLACE INTO metadata (key, value)
            VALUES ('last_import', ?)
        """, (datetime.utcnow().isoformat(),))
        
        cursor.execute("""
            INSERT OR REPLACE INTO metadata (key, value)
            VALUES ('source_coverage_file', ?)
        """, (str(coverage_file.absolute()),))
        
        cursor.execute("""
            INSERT OR REPLACE INTO metadata (key, value)
            VALUES ('total_tests', ?)
        """, (str(len(set(k[0] for k in test_file_ranges.keys()))),))
        
        cursor.execute("""
            INSERT OR REPLACE INTO metadata (key, value)
            VALUES ('total_files', ?)
        """, (str(len(mapper.coverage_data)),))
        
        # Import skipped tests if they exist
        skipped_file = coverage_file.parent / ".delta" / "skipped_tests.json"
        if skipped_file.exists():
            import json
            try:
                with open(skipped_file, "r") as f:
                    skipped_list = json.load(f)
                for test_name in skipped_list:
                    cursor.execute("""
                        INSERT OR REPLACE INTO test_coverage_ranges (test_name, file_path, ranges)
                        VALUES (?, '__skipped__', '')
                    """, (test_name,))
            except Exception as e:
                import sys
                print(f"Warning: Could not import skipped tests: {e}", file=sys.stderr)
        
        self.conn.commit()
        
        return len(test_file_ranges)
    
    def find_tests_for_file_lines(
        self, 
        file_path: str, 
        line_numbers: Set[int]
    ) -> Set[str]:
        """
        Find all tests that cover specific lines in a file.
        
        Args:
            file_path: Path to source file
            line_numbers: Set of line numbers to check
            
        Returns:
            Set of test names that cover those lines
        """
        if not line_numbers:
            return set()
        
        cursor = self.conn.cursor()
        file_path = str(file_path)
        
        # Query for all tests covering this file
        cursor.execute("""
            SELECT test_name, ranges
            FROM test_coverage_ranges
            WHERE file_path = ?
        """, (file_path,))
        
        tests = set()
        for row in cursor.fetchall():
            ranges = RangeSet.from_compact_string(row['ranges'])
            if ranges.intersects_any(line_numbers):
                tests.add(row['test_name'])
        
        return tests
    
    def get_all_test_files(self, file_path: str) -> Set[str]:
        """
        Get all tests that cover any line in a file.
        
        Args:
            file_path: Path to source file
            
        Returns:
            Set of test names
        """
        cursor = self.conn.cursor()
        file_path = str(file_path)
        
        cursor.execute("""
            SELECT DISTINCT test_name
            FROM test_coverage_ranges
            WHERE file_path = ?
        """, (file_path,))
        
        return {row['test_name'] for row in cursor.fetchall()}
    
    def get_metadata(self) -> Dict[str, str]:
        """Get all metadata key-value pairs."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT key, value FROM metadata")
        return {row['key']: row['value'] for row in cursor.fetchall()}
    
    def get_stats(self) -> Dict[str, int]:
        """Get database statistics."""
        cursor = self.conn.cursor()
        
        cursor.execute("SELECT COUNT(DISTINCT test_name) FROM test_coverage_ranges")
        total_tests = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(DISTINCT file_path) FROM test_coverage_ranges")
        total_files = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM test_coverage_ranges")
        total_range_entries = cursor.fetchone()[0]
        
        # Calculate actual line coverage and compression ratio
        cursor.execute("SELECT ranges FROM test_coverage_ranges")
        total_lines = 0
        total_ranges = 0
        for row in cursor.fetchall():
            rs = RangeSet.from_compact_string(row['ranges'])
            total_lines += len(rs)
            total_ranges += len(rs.ranges)
        
        return {
            'total_tests': total_tests,
            'total_files': total_files,
            'total_range_entries': total_range_entries,
            'total_lines_covered': total_lines,
            'total_ranges': total_ranges,
            'compression_ratio': round(total_lines / total_ranges, 2) if total_ranges > 0 else 0,
            # Backward compatibility with old TestMappingDB
            'total_mappings': total_lines,  # Equivalent to total line coverage
        }
    
    def get_all_test_names(self) -> set:
        """
        Get all unique test names efficiently.
        
        Returns:
            Set of all test names in the database
        """
        cursor = self.conn.cursor()
        # This query benefits from the idx_test_name index
        cursor.execute("SELECT DISTINCT test_name FROM test_coverage_ranges")
        return {row[0] for row in cursor.fetchall()}
    
    def optimize(self):
        """Optimize database for query performance."""
        cursor = self.conn.cursor()
        cursor.execute("ANALYZE")
        self.conn.commit()
    
    def vacuum(self):
        """Reclaim unused space."""
        self.conn.execute("VACUUM")
    
    def is_stale(self, coverage_file: Path, max_age_hours: int = 24) -> bool:
        """Check if mapping is stale compared to coverage file."""
        if coverage_file.exists():
            coverage_mtime = coverage_file.stat().st_mtime
            if self.db_path.exists():
                db_mtime = self.db_path.stat().st_mtime
                if coverage_mtime > db_mtime:
                    return True
        
        metadata = self.get_metadata()
        if 'last_import' in metadata:
            last_import = datetime.fromisoformat(metadata['last_import'])
            age_hours = (datetime.utcnow() - last_import).total_seconds() / 3600
            if age_hours > max_age_hours:
                return True
        
        return False
