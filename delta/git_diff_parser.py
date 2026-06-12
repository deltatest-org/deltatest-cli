"""Parse git diff output to identify changed files and lines."""

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Set


@dataclass
class LineRange:
    """Represents a range of changed lines."""
    start: int
    count: int
    
    def get_lines(self) -> Set[int]:
        """Get set of all line numbers in this range."""
        if self.count == 0:
            return set()
        return set(range(self.start, self.start + self.count))


@dataclass
class FileChange:
    """Represents changes to a single file."""
    file_path: str
    is_new: bool = False
    is_deleted: bool = False
    added_lines: List[LineRange] = field(default_factory=list)
    modified_lines: List[LineRange] = field(default_factory=list)
    
    def get_all_changed_lines(self) -> Set[int]:
        """Get set of all changed line numbers."""
        lines = set()
        for line_range in self.added_lines + self.modified_lines:
            lines.update(line_range.get_lines())
        return lines


class GitDiffParser:
    """Parse git diff output to find changed files and lines."""
    
    # Regex for diff hunk header: @@ -old_start,old_count +new_start,new_count @@
    HUNK_HEADER_RE = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')
    
    def __init__(self, repo_root: Path):
        """Initialize parser with repository root."""
        self.repo_root = Path(repo_root)
    
    def _resolve_base_branch(self, base_branch: str) -> str:
        """
        Verify if a base branch exists. If not, raise ValueError.
        """
        try:
            subprocess.run(
                ["git", "rev-parse", "--verify", base_branch],
                cwd=self.repo_root,
                capture_output=True,
                check=True
            )
            return base_branch
        except subprocess.CalledProcessError:
            raise ValueError(f"Target branch '{base_branch}' does not exist in this repository.")

    def get_diff(self, base_branch: str = "master") -> str:
        """
        Get git diff output comparing current HEAD to base branch.
        
        Args:
            base_branch: Branch to compare against
            
        Returns:
            Raw diff output
        """
        try:
            if base_branch == "HEAD":
                # For pre-commit hooks, we usually want staged changes
                cmd = ["git", "diff", "--cached", "--unified=0", "HEAD"]
            else:
                resolved_base = self._resolve_base_branch(base_branch)
                # Find merge base to handle branches properly
                mb_cmd = ["git", "merge-base", resolved_base, "HEAD"]
                mb_result = subprocess.run(mb_cmd, cwd=self.repo_root, capture_output=True, text=True, check=True)
                merge_base = mb_result.stdout.strip()
                
                # Diff from merge base to working tree (includes uncommitted changes)
                cmd = ["git", "diff", "--unified=0", merge_base]
                
            result = subprocess.run(
                cmd,
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                check=True
            )
            return result.stdout
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to get git diff: {e.stderr}")
    
    def parse_diff(self, diff_output: str) -> Dict[str, FileChange]:
        """
        Parse git diff output into file changes.
        
        Args:
            diff_output: Raw git diff output
            
        Returns:
            Dict mapping file paths to FileChange objects
        """
        changes = {}
        current_file = None
        
        for line in diff_output.split('\n'):
            if line.startswith('diff --git'):
                # Reset current file
                current_file = None
                
            elif line.startswith('---'):
                # Old file path: --- a/path/to/file.py or --- /dev/null
                filepath = line[6:].strip()  # Remove '--- a/'
                if filepath == '/dev/null':
                    # File is new (will be set when we see +++ line)
                    pass
                    
            elif line.startswith('+++'):
                # New file path: +++ b/path/to/file.py or +++ /dev/null
                filepath = line[6:].strip()  # Remove '+++ b/'
                
                if filepath == '/dev/null':
                    # File was deleted
                    if current_file and current_file in changes:
                        changes[current_file].is_deleted = True
                else:
                    current_file = filepath
                    if current_file not in changes:
                        changes[current_file] = FileChange(file_path=current_file)
                    
                    # Check if file is new (previous line was --- /dev/null)
                    # We'll mark it as new if we haven't seen it before
                    
            elif line.startswith('new file mode'):
                if current_file and current_file in changes:
                    changes[current_file].is_new = True
                    
            elif line.startswith('deleted file mode'):
                if current_file and current_file in changes:
                    changes[current_file].is_deleted = True
                    
            elif line.startswith('@@') and current_file:
                # Hunk header with line numbers
                match = self.HUNK_HEADER_RE.match(line)
                if match:
                    old_start = int(match.group(1)) if match.group(1) else 0
                    old_count = int(match.group(2)) if match.group(2) else 1
                    new_start = int(match.group(3)) if match.group(3) else 0
                    new_count = int(match.group(4)) if match.group(4) else 1
                    
                    if new_count > 0:
                        line_range = LineRange(start=new_start, count=new_count)
                        
                        # If old_count is 0, these are pure additions
                        if old_count == 0:
                            changes[current_file].added_lines.append(line_range)
                        else:
                            # Otherwise, treat as modifications
                            changes[current_file].modified_lines.append(line_range)
        
        return changes
    
    def filter_python_files(self, changes: Dict[str, FileChange]) -> Dict[str, FileChange]:
        """Filter changes to only include Python files."""
        return {
            path: change
            for path, change in changes.items()
            if path.endswith('.py') and not change.is_deleted
        }
    
    def get_changed_test_files(self, changes: Dict[str, FileChange]) -> Set[str]:
        """Extract test files from changes that should always be run."""
        test_files = set()
        for path, change in changes.items():
            if self._is_test_file(path) and not change.is_deleted:
                # Return full path for pytest
                test_files.add(path)
        return test_files
    
    @staticmethod
    def _is_test_file(filepath: str) -> bool:
        """Check if filepath is a test file."""
        path = Path(filepath)
        # A file is a test file if:
        # 1. It's a Python file (.py extension)
        # 2. Its name starts with 'test_'
        # This ensures fixtures, helpers, and utilities in test directories are not mistaken for tests
        return path.suffix == '.py' and path.name.startswith('test_')


def main():
    """CLI for testing the parser."""
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: git_diff_parser.py <repo_root> [base_branch]")
        sys.exit(1)
    
    repo_root = Path(sys.argv[1])
    base_branch = sys.argv[2] if len(sys.argv) > 2 else "master"
    
    parser = GitDiffParser(repo_root)
    
    try:
        diff_output = parser.get_diff(base_branch)
        changes = parser.parse_diff(diff_output)
        python_changes = parser.filter_python_files(changes)
        
        print(f"Total files changed: {len(changes)}")
        print(f"Python files changed: {len(python_changes)}")
        print(f"Test files changed: {len(parser.get_changed_test_files(changes))}")
        print("\nChanged Python files:")
        
        for filepath, change in sorted(python_changes.items()):
            status = []
            if change.is_new:
                status.append("NEW")
            if change.is_deleted:
                status.append("DELETED")
            
            status_str = f" [{', '.join(status)}]" if status else ""
            print(f"\n  {filepath}{status_str}")
            
            all_lines = change.get_all_changed_lines()
            if all_lines:
                # Group consecutive lines into ranges for display
                sorted_lines = sorted(all_lines)
                ranges = []
                start = sorted_lines[0]
                prev = start
                
                for line_num in sorted_lines[1:]:
                    if line_num != prev + 1:
                        ranges.append(f"{start}-{prev}" if start != prev else str(start))
                        start = line_num
                    prev = line_num
                ranges.append(f"{start}-{prev}" if start != prev else str(start))
                
                print(f"    Changed lines: {', '.join(ranges)}")
                
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
