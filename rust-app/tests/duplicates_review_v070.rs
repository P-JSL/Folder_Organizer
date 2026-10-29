//! New regressions for sampling, explicit partial coverage and authenticated results.
use folder_organizer_rust::core::{self, ScanOptions};
use std::{
    cell::RefCell,
    fs::{self, OpenOptions},
    io::{Seek, SeekFrom, Write},
    path::PathBuf,
    sync::atomic::{AtomicBool, Ordering},
};

const MIB: usize = 1024 * 1024;
const SAMPLE: usize = 64 * 1024;

struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        let mut random = [0_u8; 12];
        getrandom::getrandom(&mut random).unwrap();
        let id: String = random.iter().map(|n| format!("{n:02x}")).collect();
        let root = std::env::temp_dir().join(format!("organizer-duplicates-v070-{id}"));
        fs::create_dir(&root).unwrap();
        Self(root)
    }
    fn put(&self, name: &str, bytes: &[u8]) -> PathBuf {
        let path = self.0.join(name);
        fs::write(&path, bytes).unwrap();
        path
    }
    fn scan(&self) -> core::ScanResult {
        core::scan(
            &self.0,
            &ScanOptions::default(),
            &AtomicBool::new(false),
            None,
        )
        .unwrap()
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn names(group: &core::DuplicateGroup) -> Vec<String> {
    group
        .paths
        .iter()
        .map(|p| p.file_name().unwrap().to_string_lossy().into_owned())
        .collect()
}

#[test]
fn distinct_first_middle_and_end_samples_avoid_all_full_file_reads() {
    let fixture = Fixture::new();
    for pattern in 0..8 {
        let mut bytes = vec![0_u8; MIB];
        for (bit, offset) in [0, (MIB - SAMPLE) / 2, MIB - SAMPLE]
            .into_iter()
            .enumerate()
        {
            bytes[offset + 17] = ((pattern >> bit) & 1) as u8;
        }
        fixture.put(&format!("large-{pattern}.dat"), &bytes);
    }
    let result = core::analyze_duplicates(&fixture.scan(), &AtomicBool::new(false), None).unwrap();
    assert!(result.complete && result.warnings.is_empty());
    assert!(result.groups.is_empty());
    assert_eq!(result.metrics.candidate_files, 8);
    assert_eq!(result.metrics.sampled_files, 8);
    assert_eq!(result.metrics.sample_bytes, (8 * 3 * SAMPLE) as u64);
    assert_eq!(result.metrics.full_hash_files, 0);
    assert_eq!(result.metrics.full_hash_bytes, 0);
    assert_eq!(result.metrics.byte_comparison_bytes, 0);
}

#[test]
fn identical_samples_still_require_full_hashes_and_full_content_equality() {
    let fixture = Fixture::new();
    let original = vec![b'a'; MIB];
    let mut changed = original.clone();
    changed[SAMPLE + 37] = b'b'; // outside all three sample ranges
    fixture.put("a.dat", &original);
    fixture.put("b.dat", &changed);
    fixture.put("c.dat", &original);
    let result = core::analyze_duplicates(&fixture.scan(), &AtomicBool::new(false), None).unwrap();
    assert!(result.complete);
    assert_eq!(result.metrics.sampled_files, 3);
    assert_eq!(result.metrics.full_hash_files, 3);
    assert_eq!(result.metrics.full_hash_bytes, (3 * MIB) as u64);
    assert_eq!(result.groups.len(), 1);
    assert_eq!(names(&result.groups[0]), ["a.dat", "c.dat"]);
    core::validate_group(&result.groups[0]).unwrap();
}

#[test]
fn sampled_large_duplicates_beyond_content_cache_are_rechecked_byte_for_byte() {
    let fixture = Fixture::new();
    let size = 5 * MIB;
    let original = vec![b'x'; size];
    let mut distinct = original.clone();
    distinct[SAMPLE + 9] = b'y';
    fixture.put("a.dat", &original);
    fixture.put("b.dat", &original);
    fixture.put("different.dat", &distinct);
    let result = core::analyze_duplicates(&fixture.scan(), &AtomicBool::new(false), None).unwrap();
    assert!(result.complete);
    assert_eq!(result.metrics.full_hash_files, 3);
    assert_eq!(result.metrics.full_hash_bytes, (3 * size) as u64);
    assert_eq!(result.metrics.byte_comparison_bytes, (2 * size) as u64);
    assert_eq!(result.groups.len(), 1);
    assert_eq!(names(&result.groups[0]), ["a.dat", "b.dat"]);
    core::validate_group(&result.groups[0]).unwrap();
}

#[test]
fn missing_file_is_an_explicit_partial_result_while_healthy_groups_stay_authenticated() {
    let fixture = Fixture::new();
    fixture.put("a.dat", b"same verified bytes");
    fixture.put("b.dat", b"same verified bytes");
    let missing = fixture.put("gone.dat", b"same verified bytes");
    let scan = fixture.scan();
    fs::remove_file(&missing).unwrap();
    let result = core::analyze_duplicates(&scan, &AtomicBool::new(false), None).unwrap();
    assert!(!result.complete);
    assert_eq!(result.warning_count, 1);
    assert!(!result.warnings_truncated);
    assert_eq!(result.warnings[0].path.file_name().unwrap(), "gone.dat");
    assert_eq!(result.warnings[0].phase, "snapshot");
    assert_eq!(result.metrics.skipped_files, 1);
    assert_eq!(result.groups.len(), 1);
    assert_eq!(names(&result.groups[0]), ["a.dat", "b.dat"]);
    core::validate_group(&result.groups[0]).unwrap();
    assert!(core::find_duplicates(&scan, &AtomicBool::new(false), None).is_err());
    assert!(!fixture.0.join("delete_files").exists());
}

#[test]
fn hardlink_aliases_do_not_add_sample_reads_or_duplicate_deletion_targets() {
    let fixture = Fixture::new();
    let source = fixture.put("a.dat", &vec![b'z'; MIB]);
    fixture.put("b.dat", &vec![b'z'; MIB]);
    fs::hard_link(&source, fixture.0.join("alias.dat")).unwrap();
    let result = core::analyze_duplicates(&fixture.scan(), &AtomicBool::new(false), None).unwrap();
    assert!(result.complete);
    assert_eq!(result.metrics.hardlink_aliases, 1);
    assert_eq!(result.metrics.sampled_files, 2);
    assert_eq!(result.metrics.sample_bytes, (2 * 3 * SAMPLE) as u64);
    assert_eq!(result.groups.len(), 1);
    assert_eq!(result.groups[0].paths.len(), 2);
    let identities: std::collections::HashSet<_> = result.groups[0]
        .paths
        .iter()
        .map(|path| {
            let snapshot = core::snapshot(path).unwrap();
            (snapshot.device, snapshot.inode)
        })
        .collect();
    assert_eq!(identities.len(), 2);
}

#[test]
fn cancellation_during_sampling_returns_no_successful_analysis_and_changes_no_sources() {
    let fixture = Fixture::new();
    let bytes = vec![b'q'; MIB];
    let first = fixture.put("a.dat", &bytes);
    let second = fixture.put("b.dat", &bytes);
    let cancel = AtomicBool::new(false);
    let callback = |progress: core::Progress| {
        if progress.phase == "duplicates_sample" && progress.completed == 1 {
            cancel.store(true, Ordering::Relaxed);
        }
    };
    assert!(core::analyze_duplicates(&fixture.scan(), &cancel, Some(&callback)).is_err());
    assert!(cancel.load(Ordering::Relaxed));
    assert_eq!(fs::read(first).unwrap(), bytes);
    assert_eq!(fs::read(second).unwrap(), bytes);
    assert!(!fixture.0.join("delete_files").exists());
}

#[test]
fn file_changed_after_its_sample_is_warned_and_never_sealed_into_a_group() {
    let fixture = Fixture::new();
    for name in ["a.dat", "b.dat", "c.dat"] {
        fixture.put(name, &vec![b'm'; MIB]);
    }
    let changed = RefCell::new(None::<PathBuf>);
    let callback = |progress: core::Progress| {
        if progress.phase == "duplicates_sample"
            && progress.completed == 1
            && changed.borrow().is_none()
        {
            let path = fixture.0.join(&progress.message);
            let mut file = OpenOptions::new().write(true).open(&path).unwrap();
            file.seek(SeekFrom::Start((SAMPLE + 13) as u64)).unwrap();
            file.write_all(b"!").unwrap();
            file.sync_all().unwrap();
            *changed.borrow_mut() = Some(path);
        }
    };
    let result =
        core::analyze_duplicates(&fixture.scan(), &AtomicBool::new(false), Some(&callback))
            .unwrap();
    let changed = changed.into_inner().unwrap();
    assert!(!result.complete);
    assert!(result
        .warnings
        .iter()
        .any(|warning| warning.path.file_name() == changed.file_name()));
    assert!(result.groups.iter().all(|group| group
        .paths
        .iter()
        .all(|path| path.file_name() != changed.file_name())));
    assert_eq!(fs::read(changed).unwrap()[SAMPLE + 13], b'!');
    for group in result.groups {
        core::validate_group(&group).unwrap();
    }
}

#[test]
fn warning_details_are_bounded_but_scope_and_skipped_file_count_remain_honest() {
    let fixture = Fixture::new();
    fixture.put("a.dat", b"verified");
    fixture.put("b.dat", b"verified");
    let mut scan = fixture.scan();
    let template = scan.files[0].clone();
    for index in 0..1003 {
        let mut missing = template.clone();
        missing.relative_path = format!("missing-{index}.dat");
        missing.path = scan.root.join(&missing.relative_path);
        missing.name = missing.relative_path.clone();
        scan.files.push(missing);
    }
    let result = core::analyze_duplicates(&scan, &AtomicBool::new(false), None).unwrap();
    assert!(!result.complete);
    assert_eq!(result.warning_count, 1003);
    assert!(result.warnings_truncated);
    assert_eq!(result.warnings.len(), 1000);
    assert_eq!(result.metrics.skipped_files, 1003);
    assert_eq!(result.groups.len(), 1);
    core::validate_group(&result.groups[0]).unwrap();
}
