"""Pytest plugin for Delta (smart test selection and duration tracking)."""
import json
import sys
from pathlib import Path

# Stores test_node_id -> duration_ms
test_durations = {}


def pytest_addoption(parser):
    """Add Delta command line options."""
    group = parser.getgroup("delta")
    group.addoption(
        "--delta",
        action="store_true",
        default=False,
        help="Enable Delta smart test selection (run only affected tests)",
    )
    group.addoption(
        "--delta-local", "--delta-no-remote",
        action="store_true",
        default=False,
        help="Do not connect to remote mapping, only use local database",
    )
    group.addoption(
        "--delta-base",
        action="store",
        default="master",
        help="Base branch for Delta git diffing (default: master)",
    )
    group.addoption(
        "--delta-select-file",
        action="store",
        default=None,
        help="Path to a JSON file containing specific tests to run (used for xdist large test lists)",
    )


def pytest_collection_modifyitems(session, config, items):
    """Filter collected tests if --delta or --delta-select-file is enabled."""
    select_file = config.getoption("--delta-select-file")
    if not config.getoption("--delta") and not select_file:
        return

    from delta.config import Config
    from delta.cloud_mapping_db import CloudMappingDB
    from delta.test_mapping_db_v2 import TestMappingDBV2
    from delta.cli import DeltaRunner

    repo_root = Path(config.rootdir).resolve()
    affected_tests = set()
    mapping_db_obj = None

    if select_file:
        try:
            with open(select_file, "r") as f:
                affected_tests = set(json.load(f))
        except Exception as e:
            print(f"Error loading delta select file {select_file}: {e}", file=sys.stderr)
            return
    else:
        # ── Mapping source detection ───────────────────────────────────────────
        # 1. Try Cloud Mode
        cfg = Config.load()
        use_cloud = cfg.is_cloud_enabled and bool(cfg.cloud.repo_id) and not config.getoption("--delta-local")

        if use_cloud:
            print(f"Mapping Service: Delta Cloud (repo {cfg.cloud.repo_id[:8]}...)", file=sys.stderr)
            mapping_db_obj = CloudMappingDB(cfg.cloud)
        else:
            # 2. Try Local V2 Mapping DB
            mapping_db = repo_root / ".delta" / "test_mapping.db"
            legacy_db = repo_root / ".test_mapping.db"
            if not mapping_db.exists() and legacy_db.exists():
                mapping_db.parent.mkdir(parents=True, exist_ok=True)
                try:
                    legacy_db.rename(mapping_db)
                except Exception:
                    mapping_db = legacy_db

            if not mapping_db.exists():
                print("No local mapping DB found. Initializing build...", file=sys.stderr)
                from delta.build_mapping_iterative import build_mapping_iteratively

                default_test_dir = "tests"
                if getattr(cfg.cloud, "test_dir", None):
                    default_test_dir = cfg.cloud.test_dir

                exit_code = build_mapping_iteratively(
                    repo_root,
                    mapping_db,
                    test_dir=default_test_dir,
                    verbose=False
                )
                if exit_code != 0:
                    print("Failed to build mapping DB.", file=sys.stderr)
                    sys.exit(exit_code)

            print(f"Local mode: {mapping_db}", file=sys.stderr)
            mapping_db_obj = TestMappingDBV2(mapping_db)
            mapping_db_obj.connect()

        try:
            base_branch = config.getoption("--delta-base")
            if base_branch == "master" and cfg.is_cloud_enabled and getattr(cfg.cloud, "branch", None):
                base_branch = cfg.cloud.branch

            # Initialize runner
            runner = DeltaRunner(
                repo_root,
                mapping_db=mapping_db_obj,
                verbose=False
            )

            # Find affected tests
            affected_tests = runner.find_affected_tests(base_branch)

            # Unmapped tests discovery: check if any of the collected items are not in the database yet
            if mapping_db_obj:
                try:
                    from delta.pre_commit_hook import get_mapped_tests, strip_test_parameters, normalize_test_path
                    mapped_tests = get_mapped_tests(mapping_db_obj, verbose=False)
                    normalized_mapped = {normalize_test_path(strip_test_parameters(t)) for t in mapped_tests}
                    
                    for item in items:
                        norm_item = normalize_test_path(strip_test_parameters(item.nodeid))
                        if norm_item not in normalized_mapped:
                            affected_tests.add(item.nodeid)
                except Exception as e:
                    print(f"Error checking for unmapped tests: {e}", file=sys.stderr)
        finally:
            if mapping_db_obj and hasattr(mapping_db_obj, 'close'):
                mapping_db_obj.close()

    # Filter items in-place
    selected_items = []
    deselected_items = []

    for item in items:
        # Check exact nodeid match
        match = item.nodeid in affected_tests

        # Check file match (nodeid prefix before ::)
        if not match:
            file_part = item.nodeid.split("::")[0]
            if file_part in affected_tests:
                match = True

        # Check fspath/path relative match
        if not match:
            try:
                item_path = getattr(item, 'path', None)
                if not item_path and hasattr(item, 'fspath'):
                    item_path = Path(item.fspath)
                if item_path:
                    rel_path = str(item_path.relative_to(repo_root))
                    if rel_path in affected_tests:
                        match = True
            except Exception:
                pass

        if match:
            selected_items.append(item)
        else:
            deselected_items.append(item)

    items[:] = selected_items
    if deselected_items:
        config.hook.pytest_deselected(items=deselected_items)


# Stores skipped test node IDs
skipped_tests = set()


def pytest_runtest_logreport(report):
    """Capture skipped tests."""
    if report.outcome == "skipped":
        skipped_tests.add(report.nodeid)


def pytest_runtest_makereport(item, call):
    """Record test execution time during the 'call' phase."""
    if call.when == "call":
        # call.duration is a float representing exact seconds
        test_durations[item.nodeid] = int(call.duration * 1000)


def pytest_sessionfinish(session, exitstatus):
    """Dump durations and skipped tests to temporary files."""
    try:
        delta_dir = Path(session.config.rootdir) / ".delta"
        delta_dir.mkdir(parents=True, exist_ok=True)
        
        # 1. Dump durations
        out_file = delta_dir / "durations.json"
        existing_durations = {}
        if out_file.exists():
            try:
                with open(out_file, "r") as f:
                    existing_durations = json.load(f)
            except Exception:
                pass
                
        existing_durations.update(test_durations)
        with open(out_file, "w") as f:
            json.dump(existing_durations, f)
            
        # 2. Dump skipped tests
        skipped_file = delta_dir / "skipped_tests.json"
        existing_skipped = []
        if skipped_file.exists():
            try:
                with open(skipped_file, "r") as f:
                    existing_skipped = json.load(f)
            except Exception:
                pass
                
        from delta.test_mapping_db_v2 import normalize_test_name
        all_skipped = set(existing_skipped)
        for test in skipped_tests:
            all_skipped.add(normalize_test_name(test))
            
        with open(skipped_file, "w") as f:
            json.dump(list(all_skipped), f)
            
    except Exception as e:
        # Silently pass if we cannot write to rootdir
        pass
