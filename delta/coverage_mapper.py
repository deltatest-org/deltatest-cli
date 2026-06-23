"""Map test coverage to changed lines using pytest-cov data."""

import sqlite3
from pathlib import Path
from typing import Dict, Set, Optional
from dataclasses import dataclass, field


@dataclass
class CoverageData:
    """Coverage information for a file."""
    file_path: str
    executed_lines: Set[int] = field(default_factory=set)
    test_contexts: Dict[int, Set[str]] = field(default_factory=dict)


class CoverageMapper:
    """Map code coverage to test cases using .coverage database."""
    
    def __init__(self, coverage_file: Path = None, repo_root: Optional[Path] = None):
        """
        Initialize mapper with coverage database.
        
        Args:
            coverage_file: Path to .coverage file (SQLite database)
            repo_root: Repository root directory (for path normalization)
        """
        self.coverage_file = coverage_file or Path(".coverage")
        self.repo_root = repo_root
        self.coverage_data: Dict[str, CoverageData] = {}
        self._loaded = False
        
    def load_coverage(self):
        """Load coverage data from .coverage SQLite database."""
        if not self.coverage_file.exists():
            raise FileNotFoundError(
                f"Coverage file not found: {self.coverage_file}\n"
                "Generate it with: delta build-mapping"
            )
        
        conn = sqlite3.connect(str(self.coverage_file))
        cursor = conn.cursor()
        
        # Check if we have the tables we need
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        
        # Check for arc (branch) or line_bits (line) coverage
        # Note: sometimes arc table exists but is empty if branch coverage is not enabled
        has_arc = False
        if 'arc' in tables:
            cursor.execute("SELECT 1 FROM arc LIMIT 1")
            if cursor.fetchone():
                has_arc = True
        
        has_line_bits = 'line_bits' in tables
        
        if 'file' not in tables or (not has_arc and not has_line_bits):
            conn.close()
            raise RuntimeError(
                "Coverage database doesn't have required tables. "
                "Make sure to run: delta build-mapping"
            )
        
        # Check for context table (required for test-to-line mapping)
        if 'context' not in tables:
            conn.close()
            raise RuntimeError(
                "Coverage database doesn't have 'context' table. "
                "This means coverage was run without context tracking.\n"
                "Delete the old .coverage file and rebuild the mapping:\n"
                "  rm .coverage\n"
                "  delta build-mapping"
            )
        
        # Get file IDs and paths
        cursor.execute("SELECT id, path FROM file")
        files = {file_id: path for file_id, path in cursor.fetchall()}
        
        # Get context IDs and names
        cursor.execute("SELECT id, context FROM context")
        contexts = {context_id: context for context_id, context in cursor.fetchall()}
        
        # Use arc (branch coverage) if available, otherwise line_bits
        if has_arc:
            # Get arc coverage with context (test names)
            # Arc table has: file_id, context_id, fromno, tono
            cursor.execute("""
                SELECT file_id, context_id, fromno, tono
                FROM arc
            """)
            
            for file_id, context_id, fromno, tono in cursor.fetchall():
                if file_id not in files:
                    continue
                    
                file_path = files[file_id]
                
                # Normalize file path
                file_path = self._normalize_path(file_path, self.repo_root)
                
                if file_path not in self.coverage_data:
                    self.coverage_data[file_path] = CoverageData(file_path=file_path)
                
                # Get context name
                context = contexts.get(context_id, '')
                
                # Extract lines from arc (both fromno and tono are line numbers)
                # -1 means entry to the function
                lines = set()
                if fromno > 0:
                    lines.add(fromno)
                if tono > 0:
                    lines.add(tono)
                
                for line_num in lines:
                    self.coverage_data[file_path].executed_lines.add(line_num)
                    
                    if line_num not in self.coverage_data[file_path].test_contexts:
                        self.coverage_data[file_path].test_contexts[line_num] = set()
                    
                    # Context format: "testname|run" or "testname|setup" or just "testname"
                    if context:
                        # Extract test name from context
                        test_name = self._extract_test_name(context)
                        if test_name:
                            self.coverage_data[file_path].test_contexts[line_num].add(test_name)
        else:
            # Fallback to line_bits (line coverage)
            cursor.execute("""
                SELECT file_id, context_id, numbits
                FROM line_bits
            """)
            
            for file_id, context_id, numbits in cursor.fetchall():
                if file_id not in files:
                    continue
                    
                file_path = files[file_id]
                
                # Normalize file path
                file_path = self._normalize_path(file_path, self.repo_root)
                
                if file_path not in self.coverage_data:
                    self.coverage_data[file_path] = CoverageData(file_path=file_path)
                
                # Get context name
                context = contexts.get(context_id, '')
                
                # Decode bit-packed line numbers
                lines = self._decode_lines(numbits, 0)
                
                for line_num in lines:
                    self.coverage_data[file_path].executed_lines.add(line_num)
                    
                    if line_num not in self.coverage_data[file_path].test_contexts:
                        self.coverage_data[file_path].test_contexts[line_num] = set()
                    
                    # Context format: "testname|run" or just "testname"
                    if context:
                        # Extract test name from context
                        test_name = self._extract_test_name(context)
                        if test_name:
                            self.coverage_data[file_path].test_contexts[line_num].add(test_name)
        
        conn.close()
        self._loaded = True
    
    @staticmethod
    def _decode_lines(numbits: bytes, number: int) -> Set[int]:
        """
        Decode line numbers from coverage database bit encoding.
        
        The coverage.py database stores line numbers in a compressed format:
        - number: base line number
        - numbits: bit-packed offsets from base
        """
        lines = set()
        
        if numbits:
            # Bit-packed format: each bit represents an offset from 'number'
            for byte_idx, byte_val in enumerate(numbits):
                for bit_idx in range(8):
                    if byte_val & (1 << bit_idx):
                        line_num = number + (byte_idx * 8) + bit_idx
                        lines.add(line_num)
        else:
            # Single line number
            if number > 0:
                lines.add(number)
        
        return lines
    
    @staticmethod
    def _extract_test_name(context: str) -> str:
        """Extract test name from coverage context string."""
        if not context:
            return ""
        
        # Context can be:
        # - "test_file.py::TestClass::test_method|run"
        # - "test_file.py::test_function"
        # - Just the test path
        
        # Remove "|run" suffix if present
        if '|' in context:
            context = context.split('|')[0]
        
        return context
    
    def find_tests_for_lines(
        self, 
        file_path: str, 
        changed_lines: Set[int]
    ) -> Set[str]:
        """
        Find all tests that executed any of the changed lines.
        
        Args:
            file_path: Path to source file
            changed_lines: Set of changed line numbers
            
        Returns:
            Set of test names that cover those lines
        """
        if not self._loaded:
            self.load_coverage()
        
        # Normalize file path for lookup
        normalized_path = self._normalize_path(file_path)
        
        # Try to find coverage data with path matching
        coverage_data = None
        for stored_path in self.coverage_data.keys():
            if self._paths_match(normalized_path, stored_path):
                coverage_data = self.coverage_data[stored_path]
                break
        
        if not coverage_data:
            return set()
        
        # Collect all tests that executed any of the changed lines
        tests = set()
        for line_num in changed_lines:
            if line_num in coverage_data.test_contexts:
                tests.update(coverage_data.test_contexts[line_num])
        
        return tests
    
    @staticmethod
    def _normalize_path(path: str, repo_root: Optional[Path] = None) -> str:
        """
        Normalize file path for comparison.
        
        Removes common prefixes and standardizes separators.
        
        Args:
            path: File path to normalize
            repo_root: Repository root to use for making paths relative
        """
        # Convert to forward slashes
        path = path.replace('\\', '/')
        
        # Try to make relative to repo_root if provided (preferred)
        if repo_root:
            try:
                repo_root_str = str(repo_root.resolve()).replace('\\', '/')
                if path.startswith(repo_root_str):
                    return Path(path).relative_to(repo_root.resolve()).as_posix()
            except Exception:
                pass
        
        import os
        # Fallback to os.getcwd()
        try:
            cwd = os.getcwd().replace('\\', '/')
            if path.startswith(cwd):
                path = os.path.relpath(path, cwd).replace('\\', '/')
        except Exception:
            pass
        
        # Remove absolute path prefixes
        if path.startswith('/'):
            parts = Path(path).parts
            # Find the first meaningful directory (not /, Users, home, etc.)
            for i, part in enumerate(parts):
                if part in ('src',):
                    path = '/'.join(parts[i:])
                    break
        
        return path
    
    @staticmethod
    def _paths_match(path1: str, path2: str) -> bool:
        """Check if two paths refer to the same file."""
        # Exact match
        if path1 == path2:
            return True
        
        # Suffix matching (handles absolute vs relative paths)
        if path1.endswith(path2) or path2.endswith(path1):
            return True
        
        # Compare just the filename and parent directory
        p1_parts = Path(path1).parts[-2:] if len(Path(path1).parts) >= 2 else Path(path1).parts
        p2_parts = Path(path2).parts[-2:] if len(Path(path2).parts) >= 2 else Path(path2).parts
        
        return p1_parts == p2_parts


def main():
    """CLI for testing the mapper."""
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: coverage_mapper.py <coverage_file> [file_path] [line_numbers...]")
        print("\nExample:")
        print("  coverage_mapper.py .coverage src/myfile.py 10 20 30")
        sys.exit(1)
    
    coverage_file = Path(sys.argv[1])
    mapper = CoverageMapper(coverage_file)
    
    try:
        mapper.load_coverage()
        
        print(f"Loaded coverage for {len(mapper.coverage_data)} files")
        
        if len(sys.argv) >= 3:
            # Look up specific file and lines
            file_path = sys.argv[2]
            line_numbers = set(map(int, sys.argv[3:])) if len(sys.argv) > 3 else set()
            
            if line_numbers:
                tests = mapper.find_tests_for_lines(file_path, line_numbers)
                print(f"\nTests covering {file_path} lines {sorted(line_numbers)}:")
                for test in sorted(tests):
                    print(f"  - {test}")
            else:
                print(f"\nCoverage data for {file_path}:")
                for path, data in mapper.coverage_data.items():
                    if file_path in path:
                        print(f"  Executed lines: {sorted(data.executed_lines)[:10]}...")
                        print(f"  Total tests: {len(set().union(*data.test_contexts.values()))}")
        else:
            # Show summary
            print("\nCovered files:")
            for path in sorted(mapper.coverage_data.keys())[:20]:
                data = mapper.coverage_data[path]
                all_tests = set().union(*data.test_contexts.values()) if data.test_contexts else set()
                print(f"  {path}: {len(data.executed_lines)} lines, {len(all_tests)} tests")
            
            if len(mapper.coverage_data) > 20:
                print(f"  ... and {len(mapper.coverage_data) - 20} more files")
                
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
