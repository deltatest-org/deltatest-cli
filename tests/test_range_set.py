"""Tests for RangeSet class."""

# pyrefly: ignore [missing-import]
import pytest
from delta.range_set import RangeSet


class TestRangeSetBasics:
    """Test basic RangeSet operations."""
    
    def test_empty_rangeset(self):
        """Test empty range set."""
        rs = RangeSet()
        assert len(rs.ranges) == 0
        assert len(rs) == 0
        assert not rs.contains(1)
    
    def test_single_line(self):
        """Test single line range."""
        rs = RangeSet()
        rs.add_range(5, 5)
        assert rs.ranges == [(5, 5)]
        assert len(rs) == 1
        assert rs.contains(5)
        assert not rs.contains(4)
        assert not rs.contains(6)
    
    def test_single_range(self):
        """Test single multi-line range."""
        rs = RangeSet()
        rs.add_range(1, 5)
        assert rs.ranges == [(1, 5)]
        assert len(rs) == 5
        assert rs.contains(1)
        assert rs.contains(3)
        assert rs.contains(5)
        assert not rs.contains(0)
        assert not rs.contains(6)


class TestRangeMerging:
    """Test range merging logic."""
    
    def test_adjacent_ranges_merge(self):
        """Test that adjacent ranges are merged."""
        rs = RangeSet()
        rs.add_range(1, 5)
        rs.add_range(6, 10)
        assert rs.ranges == [(1, 10)]
    
    def test_overlapping_ranges_merge(self):
        """Test that overlapping ranges are merged."""
        rs = RangeSet()
        rs.add_range(1, 5)
        rs.add_range(3, 8)
        assert rs.ranges == [(1, 8)]
    
    def test_gap_preserves_separate_ranges(self):
        """Test that ranges with gaps stay separate."""
        rs = RangeSet()
        rs.add_range(1, 5)
        rs.add_range(10, 15)
        assert rs.ranges == [(1, 5), (10, 15)]
        assert len(rs) == 11
    
    def test_add_lines_merges_adjacent(self):
        """Test adding individual lines merges adjacent ranges."""
        rs = RangeSet()
        rs.add_lines({1, 2, 3, 5, 6, 7, 10})
        assert rs.ranges == [(1, 3), (5, 7), (10, 10)]
    
    def test_multiple_merges(self):
        """Test multiple ranges merging together."""
        rs = RangeSet()
        rs.add_range(1, 3)
        rs.add_range(10, 12)
        rs.add_range(20, 22)
        rs.add_range(4, 15)  # This should merge first two ranges (3 and 4 are adjacent)
        assert rs.ranges == [(1, 15), (20, 22)]


class TestRangeRemoval:
    """Test range removal and splitting."""
    
    def test_remove_middle_splits_range(self):
        """Test removing middle of range splits it."""
        rs = RangeSet()
        rs.add_range(1, 10)
        rs.remove_range(5, 5)
        assert rs.ranges == [(1, 4), (6, 10)]
    
    def test_remove_start(self):
        """Test removing start of range."""
        rs = RangeSet()
        rs.add_range(1, 10)
        rs.remove_range(1, 3)
        assert rs.ranges == [(4, 10)]
    
    def test_remove_end(self):
        """Test removing end of range."""
        rs = RangeSet()
        rs.add_range(1, 10)
        rs.remove_range(8, 10)
        assert rs.ranges == [(1, 7)]
    
    def test_remove_entire_range(self):
        """Test removing entire range."""
        rs = RangeSet()
        rs.add_range(1, 10)
        rs.remove_range(1, 10)
        assert rs.ranges == []
    
    def test_remove_multiple_ranges(self):
        """Test removing across multiple ranges."""
        rs = RangeSet()
        rs.add_range(1, 5)
        rs.add_range(10, 15)
        rs.add_range(20, 25)
        rs.remove_range(12, 22)
        assert rs.ranges == [(1, 5), (10, 11), (23, 25)]


class TestSetOperations:
    """Test set operations."""
    
    def test_intersection(self):
        """Test intersection of two range sets."""
        rs1 = RangeSet()
        rs1.add_range(1, 10)
        rs1.add_range(20, 30)
        
        rs2 = RangeSet()
        rs2.add_range(5, 15)
        rs2.add_range(25, 35)
        
        result = rs1.intersection(rs2)
        assert result.ranges == [(5, 10), (25, 30)]
    
    def test_union(self):
        """Test union of two range sets."""
        rs1 = RangeSet()
        rs1.add_range(1, 5)
        rs1.add_range(10, 15)
        
        rs2 = RangeSet()
        rs2.add_range(3, 8)
        rs2.add_range(12, 18)
        
        result = rs1.union(rs2)
        assert result.ranges == [(1, 8), (10, 18)]
    
    def test_intersects_any(self):
        """Test checking if any lines intersect."""
        rs = RangeSet()
        rs.add_range(1, 5)
        rs.add_range(10, 15)
        
        assert rs.intersects_any({3, 20})  # 3 is in range
        assert rs.intersects_any({12})     # 12 is in range
        assert not rs.intersects_any({6, 7, 8, 20})  # None in range


class TestSerialization:
    """Test serialization and deserialization."""
    
    def test_to_compact_string(self):
        """Test converting to compact string."""
        rs = RangeSet()
        rs.add_range(1, 5)
        rs.add_range(10, 12)
        rs.add_range(20, 20)
        assert rs.to_compact_string() == "1-5,10-12,20"
    
    def test_from_compact_string(self):
        """Test parsing from compact string."""
        rs = RangeSet.from_compact_string("1-5,10-12,20")
        assert rs.ranges == [(1, 5), (10, 12), (20, 20)]
    
    def test_roundtrip(self):
        """Test serialization roundtrip."""
        original = RangeSet()
        original.add_lines({1, 2, 3, 5, 6, 7, 10, 15, 16, 17, 18})
        
        serialized = original.to_compact_string()
        restored = RangeSet.from_compact_string(serialized)
        
        assert original.ranges == restored.ranges
    
    def test_empty_string(self):
        """Test parsing empty string."""
        rs = RangeSet.from_compact_string("")
        assert rs.ranges == []
        
        rs = RangeSet.from_compact_string("  ")
        assert rs.ranges == []


class TestEdgeCases:
    """Test edge cases and error conditions."""
    
    def test_reversed_range_normalized(self):
        """Test that reversed ranges are normalized."""
        rs = RangeSet()
        rs.add_range(10, 1)  # Reversed
        assert rs.ranges == [(1, 10)]
    
    def test_large_range(self):
        """Test handling large ranges."""
        rs = RangeSet()
        rs.add_range(1, 1000000)
        assert len(rs) == 1000000
        assert rs.contains(500000)
    
    def test_many_small_ranges(self):
        """Test many small ranges."""
        rs = RangeSet()
        # Add 1000 single-line ranges with gaps
        for i in range(0, 10000, 10):
            rs.add_range(i, i)
        assert len(rs.ranges) == 1000
        assert len(rs) == 1000


class TestRealWorldScenario:
    """Test realistic test coverage scenarios."""
    
    def test_pytest_coverage_pattern(self):
        """Test typical pytest coverage pattern."""
        # Simulate coverage data: function definitions, body, and random executions
        rs = RangeSet()
        
        # Function 1: lines 10-25
        rs.add_lines(set(range(10, 26)))
        
        # Function 2: lines 30-45
        rs.add_lines(set(range(30, 46)))
        
        # Some random lines (maybe imports, globals)
        rs.add_lines({1, 2, 3, 5, 50, 51, 52})
        
        # Result should be well-compressed
        assert len(rs.ranges) == 5  # Should merge into 5 ranges
        expected_lines = len(range(10, 26)) + len(range(30, 46)) + 7
        assert len(rs) == expected_lines
    
    def test_compression_ratio(self):
        """Test compression ratio for realistic data."""
        # Simulate 10,000 lines with typical coverage pattern
        rs = RangeSet()
        
        # 100 functions, 50 lines each, with 10-line gaps
        for func_start in range(0, 6000, 60):
            rs.add_lines(set(range(func_start, func_start + 50)))
        
        # Should have ~100 ranges instead of 5000 lines
        assert len(rs.ranges) < 200
        assert len(rs) == 5000  # 100 functions * 50 lines
        
        # Compression ratio should be > 25x
        compression_ratio = len(rs) / len(rs.ranges)
        assert compression_ratio > 25
