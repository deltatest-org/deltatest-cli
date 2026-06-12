"""Efficient range-based storage for line numbers."""

from typing import List, Tuple, Set


class RangeSet:
    """Efficient range-based storage with consistency maintenance."""
    
    def __init__(self, ranges: List[Tuple[int, int]] = None):
        """Initialize with optional list of (start, end) ranges."""
        self.ranges: List[Tuple[int, int]] = []
        if ranges:
            for start, end in ranges:
                self.add_range(start, end)
    
    def add_lines(self, lines: Set[int]) -> None:
        """Add individual line numbers, maintaining range consistency."""
        for line in sorted(lines):
            self.add_range(line, line)
    
    def add_range(self, start: int, end: int) -> None:
        """Add a range, merging with overlapping/adjacent ranges."""
        if start > end:
            start, end = end, start
        
        # Find ranges that overlap or are adjacent to new range
        merged_start = start
        merged_end = end
        new_ranges = []
        merged = False
        
        for r_start, r_end in self.ranges:
            # Check if ranges overlap or are adjacent (can be merged)
            if r_end < start - 1:
                # This range is completely before new range
                new_ranges.append((r_start, r_end))
            elif r_start > end + 1:
                # This range is completely after new range
                if not merged:
                    new_ranges.append((merged_start, merged_end))
                    merged = True
                new_ranges.append((r_start, r_end))
            else:
                # Ranges overlap or are adjacent - merge them
                merged_start = min(merged_start, r_start)
                merged_end = max(merged_end, r_end)
        
        if not merged:
            new_ranges.append((merged_start, merged_end))
        
        self.ranges = new_ranges
    
    def remove_lines(self, lines: Set[int]) -> None:
        """Remove individual line numbers, splitting ranges if needed."""
        for line in sorted(lines):
            self.remove_range(line, line)
    
    def remove_range(self, start: int, end: int) -> None:
        """Remove a range, splitting existing ranges if needed."""
        if start > end:
            start, end = end, start
        
        new_ranges = []
        
        for r_start, r_end in self.ranges:
            if r_end < start or r_start > end:
                # No overlap - keep range as is
                new_ranges.append((r_start, r_end))
            else:
                # Overlap - split the range
                if r_start < start:
                    # Keep the part before removed range
                    new_ranges.append((r_start, start - 1))
                if r_end > end:
                    # Keep the part after removed range
                    new_ranges.append((end + 1, r_end))
        
        self.ranges = new_ranges
    
    def contains(self, line: int) -> bool:
        """Check if a line number is covered by any range."""
        # Binary search for efficiency
        left, right = 0, len(self.ranges) - 1
        
        while left <= right:
            mid = (left + right) // 2
            start, end = self.ranges[mid]
            
            if line < start:
                right = mid - 1
            elif line > end:
                left = mid + 1
            else:
                return True
        
        return False
    
    def intersects_any(self, line_numbers: Set[int]) -> bool:
        """Check if any of the line numbers are covered."""
        for line in line_numbers:
            if self.contains(line):
                return True
        return False
    
    def intersection(self, other: 'RangeSet') -> 'RangeSet':
        """Return intersection of two range sets."""
        result = RangeSet()
        
        i = j = 0
        while i < len(self.ranges) and j < len(other.ranges):
            s1, e1 = self.ranges[i]
            s2, e2 = other.ranges[j]
            
            # Find overlap
            overlap_start = max(s1, s2)
            overlap_end = min(e1, e2)
            
            if overlap_start <= overlap_end:
                result.add_range(overlap_start, overlap_end)
            
            # Advance the range that ends first
            if e1 < e2:
                i += 1
            else:
                j += 1
        
        return result
    
    def union(self, other: 'RangeSet') -> 'RangeSet':
        """Return union of two range sets."""
        result = RangeSet(self.ranges[:])
        for start, end in other.ranges:
            result.add_range(start, end)
        return result
    
    def to_list(self) -> List[Tuple[int, int]]:
        """Export as list of (start, end) tuples."""
        return self.ranges[:]
    
    def to_compact_string(self) -> str:
        """Export as compact string representation: '1-5,10-12,20'."""
        parts = []
        for start, end in self.ranges:
            if start == end:
                parts.append(str(start))
            else:
                parts.append(f"{start}-{end}")
        return ','.join(parts)
    
    @classmethod
    def from_compact_string(cls, s: str) -> 'RangeSet':
        """Import from compact string: '1-5,10-12,20'."""
        if not s or not s.strip():
            return cls()
        
        ranges = []
        for part in s.split(','):
            part = part.strip()
            if '-' in part:
                start, end = part.split('-', 1)
                ranges.append((int(start), int(end)))
            else:
                val = int(part)
                ranges.append((val, val))
        
        return cls(ranges)
    
    def __len__(self) -> int:
        """Return total number of lines covered."""
        return sum(end - start + 1 for start, end in self.ranges)
    
    def __repr__(self) -> str:
        return f"RangeSet({self.ranges})"
