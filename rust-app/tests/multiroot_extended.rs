//! New contracts for read-only multi-folder selection and shared exact analysis.
use folder_organizer_rust::{core, multiroot};
use std::{
    fs,
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, Ordering},
        Mutex,
    },
};

#[test]
fn terminal_progress_cancellation_cannot_report_complete() {
    let fixture = Fixture::new();
    for name in ["a.bin", "b.bin"] {
        fs::write(fixture.0.join(name), b"already read exact bytes").unwrap();
    }
    let cancel = AtomicBool::new(false);
    let at_completion = |event: multiroot::MultiProgress| {
        if event.phase == "duplicates" && event.message.contains("내용이 완전히") {
            cancel.store(true, Ordering::Relaxed);
        }
    };
    let report = multiroot::analyze(
        &[fixture.0.clone()],
        &core::ScanOptions::default(),
        true,
        2,
        &cancel,
        Some(&at_completion),
    )
    .unwrap();
    assert!(cancel.load(Ordering::Relaxed));
    assert!(report.inventory_complete);
    assert!(report.cancelled && !report.complete);
    assert_eq!(
        fs::read(fixture.0.join("a.bin")).unwrap(),
        b"already read exact bytes"
    );
}

struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        let mut id = [0_u8; 12];
        getrandom::getrandom(&mut id).unwrap();
        let id: String = id.iter().map(|b| format!("{b:02x}")).collect();
        let path = std::env::temp_dir().join(format!("organizer-multiroot-{id}"));
        fs::create_dir(&path).unwrap();
        Self(path)
    }
    fn folder(&self, name: &str) -> PathBuf {
        let root = self.0.join(name);
        fs::create_dir_all(&root).unwrap();
        root
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[test]
fn cross_folder_exact_results_are_byte_verified_but_never_operation_proofs() {
    let fixture = Fixture::new();
    let left = fixture.folder("한글 A");
    let right = fixture.folder("한글 B");
    let original = b"same bytes across actual folder boundaries\n";
    fs::write(left.join("keep.txt"), original).unwrap();
    fs::write(right.join("copy.txt"), original).unwrap();
    fs::write(right.join("different.txt"), vec![b'x'; original.len()]).unwrap();
    let report = multiroot::analyze(
        &[left.clone(), right.clone()],
        &core::ScanOptions::default(),
        true,
        2,
        &AtomicBool::new(false),
        None,
    )
    .unwrap();
    assert!(report.complete && report.inventory_complete);
    assert_eq!(report.files.len(), 3);
    assert_eq!(report.duplicates.groups.len(), 1);
    let group = &report.duplicates.groups[0];
    assert_eq!(group.paths.len(), 2);
    assert_eq!(group.group_source_count, 2);
    assert!(!group.action_authority && !report.action_authority);
    let serialized = serde_json::to_value(group).unwrap();
    assert!(serialized.get("proof").is_none() && serialized.get("snapshots").is_none());
    let imported: core::DuplicateGroup = serde_json::from_value(serialized).unwrap();
    assert!(core::validate_group(&imported).is_err());
    assert_eq!(fs::read(left.join("keep.txt")).unwrap(), original);
    assert_eq!(fs::read(right.join("copy.txt")).unwrap(), original);
    assert!(!left.join("delete_files").exists() && !right.join(".folder-organizer-rust").exists());
}

#[test]
fn repeated_and_nested_roots_keep_membership_without_counting_paths_twice() {
    let fixture = Fixture::new();
    let root = fixture.folder("parent");
    let child = fixture.folder("parent/child");
    fs::write(root.join("a.bin"), b"twins").unwrap();
    fs::write(child.join("b.bin"), b"twins").unwrap();
    let report = multiroot::analyze(
        &[root.clone(), root.join("."), child.clone()],
        &core::ScanOptions::default(),
        true,
        4,
        &AtomicBool::new(false),
        None,
    )
    .unwrap();
    assert!(report.complete);
    assert_eq!(report.roots.len(), 2);
    assert_eq!(report.duplicate_inputs.len(), 1);
    assert!(!report.notes.is_empty());
    assert_eq!(report.files.len(), 2);
    let nested = report
        .files
        .iter()
        .find(|f| f.path.ends_with("b.bin"))
        .unwrap();
    assert_eq!(nested.root_ids.len(), 2);
    assert_eq!(nested.owner_root_id, "root-0001");
    assert_eq!(nested.relative_paths["root-0001"], "child/b.bin");
    assert_eq!(nested.relative_paths["root-0002"], "b.bin");
    assert_eq!(report.duplicates.groups[0].paths.len(), 2);
}

#[test]
fn one_offline_root_is_partial_and_does_not_hide_healthy_cross_root_groups() {
    let fixture = Fixture::new();
    let left = fixture.folder("a");
    let right = fixture.folder("b");
    fs::write(left.join("a.txt"), b"hello").unwrap();
    fs::write(right.join("b.txt"), b"hello").unwrap();
    let missing = fixture.0.join("offline");
    let report = multiroot::analyze(
        &[left, missing, right],
        &core::ScanOptions::default(),
        true,
        2,
        &AtomicBool::new(false),
        None,
    )
    .unwrap();
    assert!(!report.complete && !report.inventory_complete);
    assert_eq!(report.duplicates.complete, Some(false));
    assert_eq!(report.roots[1].state, "failed");
    assert!(report.roots[1].error.is_some());
    assert_eq!(report.duplicates.groups.len(), 1);
    assert_eq!(
        report.duplicates.groups[0].root_ids,
        ["root-0001", "root-0003"]
    );
    assert!(report
        .duplicates
        .warnings
        .iter()
        .any(|warning| warning.phase == "inventory"));
}

#[test]
fn physical_hardlink_aliases_are_not_independent_duplicate_originals() {
    let fixture = Fixture::new();
    let left = fixture.folder("a");
    let right = fixture.folder("b");
    fs::write(left.join("original.dat"), b"one physical file").unwrap();
    fs::hard_link(left.join("original.dat"), right.join("alias.dat")).unwrap();
    let report = multiroot::analyze(
        &[left, right],
        &core::ScanOptions::default(),
        true,
        2,
        &AtomicBool::new(false),
        None,
    )
    .unwrap();
    assert!(report.complete);
    assert_eq!(report.files.len(), 2);
    assert!(report.duplicates.groups.is_empty());
    assert_eq!(report.duplicates.metrics.hardlink_aliases, 1);
}

#[test]
fn cancelled_scan_or_global_hash_never_pretends_to_be_complete() {
    let fixture = Fixture::new();
    let left = fixture.folder("a");
    let right = fixture.folder("b");
    fs::write(left.join("a.txt"), b"same").unwrap();
    fs::write(right.join("b.txt"), b"same").unwrap();
    let cancelled = AtomicBool::new(true);
    let first = multiroot::analyze(
        &[left.clone(), right.clone()],
        &core::ScanOptions::default(),
        true,
        2,
        &cancelled,
        None,
    )
    .unwrap();
    assert!(first.cancelled && !first.complete && first.duplicates.cancelled);
    assert!(first.duplicates.groups.is_empty());
    assert!(first.roots.iter().all(|root| root.state == "cancelled"));
    cancelled.store(false, Ordering::Relaxed);
    let callback = |event: multiroot::MultiProgress| {
        if event.root_id.is_empty() && event.phase == "duplicates_sample" {
            cancelled.store(true, Ordering::Relaxed);
        }
    };
    let later = multiroot::analyze(
        &[left, right],
        &core::ScanOptions::default(),
        true,
        2,
        &cancelled,
        Some(&callback),
    )
    .unwrap();
    assert!(later.cancelled && !later.complete && later.duplicates.cancelled);
    assert!(later.duplicates.groups.is_empty());
}

#[test]
fn mutation_between_overlapping_root_inventories_is_excluded_and_reported() {
    let fixture = Fixture::new();
    let root = fixture.folder("parent");
    let child = fixture.folder("parent/child");
    let victim = child.join("file.txt");
    fs::write(&victim, b"old data").unwrap();
    let changed = AtomicBool::new(false);
    let callback = |event: multiroot::MultiProgress| {
        if event.root_id == "root-0001"
            && event.phase == "scan"
            && event.total.is_some()
            && !changed.swap(true, Ordering::Relaxed)
        {
            fs::write(&victim, b"new and longer data").unwrap();
        }
    };
    let report = multiroot::analyze(
        &[root, child],
        &core::ScanOptions::default(),
        true,
        4,
        &AtomicBool::new(false),
        Some(&callback),
    )
    .unwrap();
    assert!(changed.load(Ordering::Relaxed));
    assert!(!report.complete && !report.inventory_complete);
    assert!(report.files.is_empty() && report.duplicates.groups.is_empty());
    assert!(report
        .duplicates
        .warnings
        .iter()
        .any(|warning| warning.path == victim));
    assert_eq!(fs::read(victim).unwrap(), b"new and longer data");
}

#[test]
fn inventory_only_skips_hashes_and_same_volume_scans_do_not_overlap() {
    let fixture = Fixture::new();
    let roots = [
        fixture.folder("a"),
        fixture.folder("b"),
        fixture.folder("c"),
    ];
    for root in &roots {
        fs::write(root.join("same.txt"), b"same").unwrap();
    }
    let active = Mutex::new((std::collections::HashSet::new(), 0_usize));
    let callback = |event: multiroot::MultiProgress| {
        let mut state = active.lock().unwrap();
        if event.phase == "scan_start" {
            state.0.insert(event.root_id.clone());
            state.1 = state.1.max(state.0.len());
        } else if event.phase == "scan" && event.total.is_some() {
            state.0.remove(&event.root_id);
        }
    };
    let report = multiroot::analyze(
        &roots,
        &core::ScanOptions::default(),
        false,
        4,
        &AtomicBool::new(false),
        Some(&callback),
    )
    .unwrap();
    assert!(report.complete);
    assert_eq!(report.duplicates.complete, None);
    assert!(!report.duplicates.requested && report.duplicates.groups.is_empty());
    assert_eq!(report.duplicates.metrics.full_hash_bytes, 0);
    assert_eq!(active.into_inner().unwrap().1, 1);
    let json = serde_json::to_value(report).unwrap();
    assert!(json["files"][0]["modified_ns"].is_string());
    assert_eq!(json["schema"], "folder-organizer.multiroot.v1");
}

#[cfg(unix)]
#[test]
fn selected_symlink_root_is_failed_without_traversing_its_target() {
    let fixture = Fixture::new();
    let real = fixture.folder("real");
    fs::write(real.join("untouched.txt"), b"original").unwrap();
    let alias = fixture.0.join("link");
    std::os::unix::fs::symlink(&real, &alias).unwrap();
    let report = multiroot::analyze(
        &[alias],
        &core::ScanOptions::default(),
        true,
        2,
        &AtomicBool::new(false),
        None,
    )
    .unwrap();
    assert!(!report.complete);
    assert_eq!(report.roots[0].state, "failed");
    assert!(report.files.is_empty());
    assert_eq!(fs::read(real.join("untouched.txt")).unwrap(), b"original");
}

#[test]
fn inventory_conflict_keeps_visible_root_warnings_without_requesting_duplicates() {
    let fixture = Fixture::new();
    let root = fixture.folder("parent");
    let child = fixture.folder("parent/child");
    let file = child.join("change.txt");
    fs::write(&file, b"old").unwrap();
    let changed = AtomicBool::new(false);
    let callback = |event: multiroot::MultiProgress| {
        if event.root_id == "root-0001"
            && event.phase == "scan"
            && event.total.is_some()
            && !changed.swap(true, Ordering::Relaxed)
        {
            fs::write(&file, b"changed after first scan").unwrap();
        }
    };
    let report = multiroot::analyze(
        &[root.clone(), child],
        &core::ScanOptions::default(),
        false,
        2,
        &AtomicBool::new(false),
        Some(&callback),
    )
    .unwrap();
    assert!(!report.complete && !report.inventory_complete);
    assert!(report.files.is_empty());
    assert_eq!(report.duplicates.complete, None);
    assert!(report
        .roots
        .iter()
        .all(|root| root.state == "partial" && !root.warnings.is_empty()));
    for workers in [0, 5] {
        assert!(multiroot::analyze(
            &[root.clone()],
            &core::ScanOptions::default(),
            false,
            workers,
            &AtomicBool::new(false),
            None
        )
        .is_err());
    }
    assert!(multiroot::analyze(
        &vec![root; 65],
        &core::ScanOptions::default(),
        false,
        2,
        &AtomicBool::new(false),
        None
    )
    .is_err());
}

#[test]
fn shared_kernel_preserves_single_root_warning_context_and_operation_proof_contract() {
    let fixture = Fixture::new();
    let root = fixture.folder("single");
    fs::write(root.join("a.txt"), b"same original single-root contract").unwrap();
    fs::write(root.join("b.txt"), b"same original single-root contract").unwrap();
    let cancel = AtomicBool::new(false);
    let mut scan = core::scan(&root, &core::ScanOptions::default(), &cancel, None).unwrap();
    scan.warnings.push("fixture inventory omission".into());
    let analysis = core::analyze_duplicates(&scan, &cancel, None).unwrap();
    assert!(!analysis.complete);
    assert_eq!(analysis.warning_count, 1);
    assert_eq!(analysis.warnings[0].path, scan.root);
    assert_eq!(analysis.warnings[0].phase, "inventory");
    assert_eq!(analysis.groups.len(), 1);
    core::validate_group(&analysis.groups[0]).unwrap();
    let compatible = core::find_duplicates(&scan, &cancel, None).unwrap();
    assert_eq!(compatible.len(), 1);
    scan.cancelled = true;
    assert!(core::analyze_duplicates(&scan, &cancel, None).is_err());
    assert!(core::find_duplicates(&scan, &cancel, None).is_err());
}

#[test]
fn inventory_warning_forwarding_bounds_details_without_losing_total_or_root_coverage() {
    const WARNINGS: usize = 1041;
    let fixture = Fixture::new();
    let left = fixture.folder("a");
    let right = fixture.folder("b");
    for name in ["first.txt", "second.txt"] {
        fs::write(left.join(name), b"healthy exact group remains visible").unwrap();
    }
    fs::write(
        right.join("third.txt"),
        b"healthy exact group remains visible",
    )
    .unwrap();
    let cancel = AtomicBool::new(false);
    let mut scan = core::scan(&left, &core::ScanOptions::default(), &cancel, None).unwrap();
    scan.warnings = (0..WARNINGS)
        .map(|index| format!("omitted entry {index}"))
        .collect();
    let analysis = core::analyze_duplicates(&scan, &cancel, None).unwrap();
    assert!(!analysis.complete && analysis.warnings_truncated);
    assert_eq!(analysis.warning_count, WARNINGS as u64);
    assert_eq!(analysis.warnings.len(), 1000);
    assert_eq!(analysis.warnings[999].message, "omitted entry 999");
    assert!(analysis
        .warnings
        .iter()
        .all(|warning| warning.path == scan.root && warning.phase == "inventory"));
    assert_eq!(analysis.groups.len(), 1);
    core::validate_group(&analysis.groups[0]).unwrap();
    // Real inventory omissions also exercise bounded forwarding in the
    // multi-root adapter, while retaining every original per-root warning.
    #[cfg(unix)]
    {
        for index in 0..WARNINGS {
            std::os::unix::fs::symlink(
                left.join("first.txt"),
                left.join(format!("alias-{index:04}")),
            )
            .unwrap();
        }
        let report = multiroot::analyze(
            &[left.clone(), right.clone()],
            &core::ScanOptions::default(),
            true,
            2,
            &cancel,
            None,
        )
        .unwrap();
        assert!(!report.complete && !report.inventory_complete);
        assert_eq!(report.roots[0].state, "partial");
        assert_eq!(report.roots[0].warnings.len(), WARNINGS);
        assert_eq!(report.duplicates.warning_count, WARNINGS as u64);
        assert!(report.duplicates.warnings_truncated);
        assert_eq!(report.duplicates.warnings.len(), 1000);
        assert_eq!(report.duplicates.groups.len(), 1);
        assert_eq!(report.duplicates.groups[0].paths.len(), 3);
    }
    assert_eq!(
        fs::read(left.join("first.txt")).unwrap(),
        b"healthy exact group remains visible"
    );
    assert_eq!(
        fs::read(right.join("third.txt")).unwrap(),
        b"healthy exact group remains visible"
    );
}
