"""Basic tests for git_diff_parser module."""

import pytest
from pathlib import Path
from delta.git_diff_parser import GitDiffParser, FileChange, LineRange


def test_line_range_get_lines():
    """Test LineRange.get_lines() returns correct line numbers."""
    range_obj = LineRange(start=10, count=5)
    lines = range_obj.get_lines()
    assert lines == {10, 11, 12, 13, 14}


def test_line_range_empty():
    """Test LineRange with count=0 returns empty set."""
    range_obj = LineRange(start=10, count=0)
    lines = range_obj.get_lines()
    assert lines == set()


def test_file_change_get_all_changed_lines():
    """Test FileChange aggregates all changed lines."""
    change = FileChange(
        file_path="test.py",
        added_lines=[LineRange(5, 2)],
        modified_lines=[LineRange(10, 3)]
    )
    all_lines = change.get_all_changed_lines()
    assert all_lines == {5, 6, 10, 11, 12}


def test_is_test_file():
    """Test _is_test_file correctly identifies test files."""
    parser = GitDiffParser(Path("."))
    
    assert parser._is_test_file("test_example.py")
    assert parser._is_test_file("unit_tests/test_module.py")
    assert parser._is_test_file("src/tests/test_something.py")
    assert not parser._is_test_file("src/module.py")
    assert not parser._is_test_file("utils.py")


def test_filter_python_files():
    """Test filtering to only Python files."""
    parser = GitDiffParser(Path("."))
    
    changes = {
        "src/file.py": FileChange(file_path="src/file.py"),
        "README.md": FileChange(file_path="README.md"),
        "deleted.py": FileChange(file_path="deleted.py", is_deleted=True),
        "new.py": FileChange(file_path="new.py", is_new=True),
    }
    
    python_changes = parser.filter_python_files(changes)
    
    assert "src/file.py" in python_changes
    assert "new.py" in python_changes
    assert "README.md" not in python_changes
    assert "deleted.py" not in python_changes
