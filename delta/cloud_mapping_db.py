"""
Cloud mapping database client for Delta.

Drop-in replacement for TestMappingDBV2 when a Delta API key is configured.
Makes direct API calls — no local state, no caching.
"""
import sys
from pathlib import Path
from typing import Set, List, Dict, Optional

try:
    import requests
except ImportError:
    requests = None   # will raise at init time if cloud is used

from .config import CloudConfig
from .test_mapping_db_v2 import normalize_test_name
from .range_set import RangeSet


class CloudMappingDB:
    """
    Talks to api.deltatest.dev instead of a local SQLite file.
    Interface mirrors TestMappingDBV2 so pre_commit_hook.py needs minimal changes.
    """

    def __init__(self, config: CloudConfig):
        if requests is None:
            raise ImportError(
                "The 'requests' library is required for cloud mode.\n"
                "Install it with: pip install requests"
            )
        if not config.repo_id:
            raise ValueError(
                "No repo_id configured. Run: delta track --name <your-repo>"
            )

        self.config = config
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        })
        self._api = config.api_url.rstrip("/")
        self._repo_id = config.repo_id
        self._branch = config.branch
        self.explanation = {}

    # ── Query (called by pre_commit_hook during commit) ────────────────────────

    def find_tests_for_changes(self, changes: List[Dict], branch: Optional[str] = None, commit_sha: Optional[str] = None) -> tuple[Set[str], List[str]]:
        """
        Query the cloud API for affected tests.

        Args:
            changes: [{"file": "src/auth.py", "lines": [42, 43]}, ...]
            branch: Optional branch to query mapping for (overrides default)

        Returns:
            (affected_tests: set[str], unmapped_files: list[str])
        """
        payload = {
            "branch": branch or self._branch,
            "commit_sha": commit_sha,
            "changes": changes,
        }
        try:
            resp = self.session.post(
                f"{self._api}/api/v1/repos/{self._repo_id}/query",
                json=payload,
                timeout=30,
            )
            if resp.status_code == 402:
                try:
                    err_data = resp.json()
                    msg = err_data.get("detail", "Free tier quota exceeded.")
                except Exception:
                    msg = "Free tier quota exceeded."
                print(f"Delta Cloud free tier limit reached ({msg}). Please upgrade at https://deltatest.dev/#pricing", file=sys.stderr)
                return set(), [c["file"] for c in changes]
            resp.raise_for_status()
        except requests.Timeout:
            print("Delta API timed out — falling back to running all tests", file=sys.stderr)
            return set(), [c["file"] for c in changes]
        except requests.RequestException as e:
            print(f"Delta API error: {e}", file=sys.stderr)
            return set(), [c["file"] for c in changes]

        data = resp.json()
        self.explanation = data.get("explanation", {})
        return set(data["affected_tests"]), data.get("unmapped_files", [])

    def find_tests_for_file_lines(self, file_path: str, line_numbers: Set[int], branch: Optional[str] = None, commit_sha: Optional[str] = None) -> Set[str]:
        """
        Single-file wrapper — matches TestMappingDBV2 interface used by pre_commit_hook.
        
        Args:
            file_path: Path to the source file
            line_numbers: Set of changed line numbers
            branch: Optional branch to query mapping for
            commit_sha: Optional commit sha for merge-base lookup
        """
        tests, _ = self.find_tests_for_changes(
            [{"file": file_path, "lines": sorted(line_numbers)}],
            branch=branch,
            commit_sha=commit_sha,
        )
        return tests

    # ── Push (called after test run to update mappings) ────────────────────────

    def push_coverage(
        self,
        coverage_file: Path,
        run_stats: Optional[Dict] = None,
        verbose: bool = False,
    ) -> bool:
        """
        Parse a .coverage file and push new mappings to the cloud API.

        Args:
            coverage_file: Path to the .coverage sqlite file
            run_stats:     Optional dict with tests_selected, tests_total, duration_ms, result
            verbose:       Print progress

        Returns:
            True on success, False on failure
        """
        from .coverage_mapper import CoverageMapper   # existing module
        from .push_cache import PushCache

        if not coverage_file.exists():
            if verbose:
                print(f"No coverage file at {coverage_file} — skipping cloud push")
            return False

        print("Parsing coverage data for cloud upload...", flush=True)

        mapper = CoverageMapper(coverage_file)
        try:
            mapper.load_coverage()
        except Exception as e:
            print(f"Could not load coverage: {e}", file=sys.stderr)
            return False

        # Build range-compressed mappings (test -> file -> RangeSet)
        test_file_ranges: Dict[tuple[str, str], RangeSet] = {}
        
        for file_path, coverage_data in mapper.coverage_data.items():
            for line_num, test_contexts in coverage_data.test_contexts.items():
                for test_name in test_contexts:
                    # Normalize test name to aggregate parametrized tests
                    norm_name = normalize_test_name(test_name)
                    
                    key = (norm_name, file_path)
                    if key not in test_file_ranges:
                        test_file_ranges[key] = RangeSet()
                    test_file_ranges[key].add_range(line_num, line_num)

        # Import skipped tests if they exist
        skipped_file = coverage_file.parent / ".delta" / "skipped_tests.json"
        if skipped_file.exists():
            import json
            try:
                with open(skipped_file, "r") as f:
                    skipped_list = json.load(f)
                for test_name in skipped_list:
                    norm_name = normalize_test_name(test_name)
                    key = (norm_name, "__skipped__")
                    if key not in test_file_ranges:
                        test_file_ranges[key] = RangeSet()
            except Exception as e:
                print(f"Warning: Could not import skipped tests for cloud upload: {e}", file=sys.stderr)

        delta_dir = coverage_file.parent / ".delta"
        delta_dir.mkdir(parents=True, exist_ok=True)
        
        cache_db_path = delta_dir / "push_cache.db"
        legacy_cache = coverage_file.parent / ".delta_push_cache.db"
        if not cache_db_path.exists() and legacy_cache.exists():
            try:
                legacy_cache.rename(cache_db_path)
            except Exception:
                cache_db_path = legacy_cache

        push_cache = PushCache(cache_db_path)
        cached_state = push_cache.get_cached_state(self._branch)

        import json
        durations_file = delta_dir / "durations.json"
        legacy_durations = coverage_file.parent / ".delta_durations.json"
        if not durations_file.exists() and legacy_durations.exists():
            try:
                legacy_durations.rename(durations_file)
            except Exception:
                durations_file = legacy_durations

        try:
            test_durations = json.loads(durations_file.read_text()) if durations_file.exists() else {}
        except Exception:
            test_durations = {}

        mappings = []
        for (test_name, file_path), rs in test_file_ranges.items():
            compact_ranges = rs.to_compact_string()
            key = (test_name, file_path)
            duration_ms = test_durations.get(test_name, 0)
            cached_ranges, cached_duration = cached_state.get(key, (None, 0))
            if cached_ranges != compact_ranges or (duration_ms > 0 and duration_ms != cached_duration):
                mappings.append({
                    "test_name": test_name,
                    "file_path": file_path,
                    "ranges": compact_ranges,
                    "duration_ms": duration_ms,
                })

        if not mappings:
            print("All coverage mappings are up-to-date with Delta Cloud (0 deltas)")
            return True

        print(f"Pushing {len(mappings)} delta mappings to Delta...", flush=True)

        payload_base = {
            "branch": self._branch,
        }
        if run_stats:
            payload_base["run_stats"] = run_stats

        from concurrent.futures import ThreadPoolExecutor, as_completed

        # Send in chunks of 1,000 parallelized in 10 threads
        CHUNK_SIZE = 1000
        success = True
        total_inserted = 0
        total_updated = 0
        total_tests_cloud = 0

        chunks = [mappings[i:i + CHUNK_SIZE] for i in range(0, len(mappings), CHUNK_SIZE)]
        total_chunks = len(chunks)

        def push_chunk(chunk_num, chunk_data):
            print(f"Pushing chunk {chunk_num}/{total_chunks} ({len(chunk_data)} mappings)...", flush=True)
            payload = payload_base.copy()
            payload["mappings"] = chunk_data
            resp = self.session.post(
                f"{self._api}/api/v1/repos/{self._repo_id}/coverage",
                json=payload,
                timeout=60,
            )
            if resp.status_code == 402:
                try:
                    err_data = resp.json()
                    msg = err_data.get("detail", "Free tier quota exceeded.")
                except Exception:
                    msg = "Free tier quota exceeded."
                raise requests.RequestException(
                    f"Delta Cloud free tier limit reached ({msg}). Please upgrade your plan at https://deltatest.dev/#pricing to sync large repositories."
                )
            resp.raise_for_status()
            push_cache.batch_upsert(self._branch, chunk_data)
            data = resp.json()
            inserted = data.get('inserted', 0)
            updated = data.get('updated', 0)
            t_tests = data.get('total_tests', 0)
            print(f"      Chunk {chunk_num}/{total_chunks} complete ({inserted} new, {updated} updated)", flush=True)
            return inserted, updated, t_tests

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(push_chunk, idx + 1, chunk): idx + 1 for idx, chunk in enumerate(chunks)}
            for future in as_completed(futures):
                chunk_num = futures[future]
                try:
                    inserted, updated, t_tests = future.result()
                    total_inserted += inserted
                    total_updated += updated
                    if t_tests > total_tests_cloud:
                        total_tests_cloud = t_tests
                except requests.RequestException as e:
                    print(f"Cloud push failed on chunk {chunk_num}: {e}", file=sys.stderr)
                    success = False
                    for f in futures:
                        f.cancel()
                except Exception as e:
                    if success:
                        print(f"Cloud push failed on chunk {chunk_num}: {e}", file=sys.stderr)
                        success = False
                        for f in futures:
                            f.cancel()
                
        if success:
            print(
                f"Cloud sync complete: {total_inserted} new, "
                f"{total_updated} updated, "
                f"{total_tests_cloud} total tests",
                flush=True
            )
        return success

    def get_all_test_names(self) -> Set[str]:
        """Fetch all unique test names from the cloud."""
        try:
            resp = self.session.get(
                f"{self._api}/api/v1/repos/{self._repo_id}/tests",
                timeout=10,
            )
            if resp.status_code == 402:
                print(f"Delta Cloud free tier limit reached. Please upgrade at https://deltatest.dev/#pricing", file=sys.stderr)
                return set()
            resp.raise_for_status()
            return set(resp.json())
        except Exception as e:
            print(f"Could not fetch test list from cloud: {e}", file=sys.stderr)
            return set()

    # ── Context manager support (mirrors TestMappingDBV2) ─────────────────────

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.session.close()

    def is_initialized(self) -> bool:
        """Always True for cloud mode — API handles initialization."""
        return True

    def get_stats(self) -> Dict:
        """Fetch DB stats from API."""
        try:
            resp = self.session.get(
                f"{self._api}/api/v1/repos/{self._repo_id}/stats",
                timeout=10,
            )
            if resp.status_code == 402:
                return {"total_tests": "Quota Exceeded", "files_covered": "Quota Exceeded"}
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return {"total_tests": "?", "files_covered": "?"}
