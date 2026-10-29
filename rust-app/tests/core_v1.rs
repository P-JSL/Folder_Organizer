use folder_organizer_rust::core::{
    find_duplicates, fingerprint, scan, snapshot, time_from_ns, time_ns, validate_group,
    DuplicateGroup, FileSnapshot, ScanOptions,
};
use folder_organizer_rust::similarity::{find_similar, find_similar_excluding, SimilarOptions};
use std::fs::{self, OpenOptions};
use std::path::{Path, PathBuf};
use std::sync::atomic::AtomicBool;
use std::time::Duration;

struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        let mut random = [0_u8; 12];
        getrandom::getrandom(&mut random).unwrap();
        let name = random
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>();
        let root = std::env::temp_dir().join(format!("organizer-core-{name}"));
        fs::create_dir(&root).unwrap();
        Self(root)
    }
    fn put(&self, relative: &str, bytes: &[u8]) {
        let path = self.0.join(relative);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, bytes).unwrap();
    }
    fn path(&self) -> &Path {
        &self.0
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[test]
fn inventory_preserves_dates_and_excludes_both_editions_storage_and_nas_trash() {
    let fixture = Fixture::new();
    fixture.put("readme.txt", b"ordinary");
    fixture.put("app.EXE", b"app");
    for directory in [
        ".folder-organizer",
        ".folder-organizer-rust",
        "delete_files",
        "#recycle",
        "@Recycle",
        "$Recycle.Bin",
    ] {
        fixture.put(&format!("{directory}/old.txt"), b"must not be scanned");
    }
    let result = scan(
        fixture.path(),
        &ScanOptions {
            include_hidden: true,
            ..Default::default()
        },
        &AtomicBool::new(false),
        None,
    )
    .unwrap();
    assert_eq!(result.files.len(), 2);
    assert_eq!(result.total_size(), 11);
    assert_eq!(
        result
            .files
            .iter()
            .filter(|file| file.kind == "application")
            .count(),
        1
    );
    assert!(result.files.iter().all(|file| file.modified_ns > 0));
    assert_eq!(result.metrics.visited_entries, 8);
}

#[test]
fn pre_epoch_fractional_file_times_are_preserved_in_inventory_and_snapshot() {
    let fixture = Fixture::new();
    fixture.put("old.txt", b"old file");
    let value = std::time::UNIX_EPOCH - Duration::from_millis(1250);
    assert_eq!(time_ns(value), -1_250_000_000);
    assert_eq!(time_from_ns(-1_250_000_000).unwrap(), value);
    OpenOptions::new()
        .write(true)
        .open(fixture.path().join("old.txt"))
        .unwrap()
        .set_times(fs::FileTimes::new().set_modified(value))
        .unwrap();
    let result = scan(
        fixture.path(),
        &ScanOptions::default(),
        &AtomicBool::new(false),
        None,
    )
    .unwrap();
    assert_eq!(result.files[0].modified_ns, -1_250_000_000);
    assert_eq!(
        snapshot(&result.files[0].path).unwrap().modified_ns,
        -1_250_000_000
    );
}

#[test]
fn full_windows_128_bit_file_ids_survive_json_value_reports_and_journals() {
    let original = FileSnapshot {
        size: 12,
        modified_ns: -1_250_000_000,
        changed_ns: 42,
        device: u64::MAX,
        inode: u128::MAX - 17,
    };
    let value = serde_json::to_value(&original).unwrap();
    let encoded = serde_json::to_vec(&value).unwrap();
    let decoded: FileSnapshot = serde_json::from_slice(&encoded).unwrap();
    assert_eq!(decoded, original);
    assert!(String::from_utf8(encoded)
        .unwrap()
        .contains(&(u128::MAX - 17).to_string()));
}

#[test]
fn whole_sha_and_contents_are_verified_and_export_is_not_a_deletion_proof() {
    let fixture = Fixture::new();
    fixture.put("a.txt", b"abc");
    fixture.put("b.txt", b"abc");
    fixture.put("c.txt", b"abd");
    let result = scan(
        fixture.path(),
        &ScanOptions::default(),
        &AtomicBool::new(false),
        None,
    )
    .unwrap();
    let groups = find_duplicates(&result, &AtomicBool::new(false), None).unwrap();
    assert_eq!(groups.len(), 1);
    assert_eq!(groups[0].paths.len(), 2);
    assert_eq!(
        groups[0].sha256,
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    );
    validate_group(&groups[0]).unwrap();
    let serialized = serde_json::to_string(&groups[0]).unwrap();
    assert!(!serialized.contains("proof"));
    assert!(!serialized.contains("snapshots"));
    let imported: DuplicateGroup = serde_json::from_str(&serialized).unwrap();
    assert!(validate_group(&imported).is_err());
    let mut reordered = groups[0].clone();
    reordered.paths.reverse();
    assert!(validate_group(&reordered).is_err());
}

#[test]
fn cached_exact_groups_are_not_also_reported_as_similar() {
    let fixture = Fixture::new();
    fixture.put("report-one.txt", b"one two three");
    fixture.put("report-copy.txt", b"one two three");
    let result = scan(
        fixture.path(),
        &ScanOptions::default(),
        &AtomicBool::new(false),
        None,
    )
    .unwrap();
    let groups = find_duplicates(&result, &AtomicBool::new(false), None).unwrap();
    let report = find_similar_excluding(
        &result,
        &SimilarOptions::default(),
        &groups,
        &AtomicBool::new(false),
        None,
    )
    .unwrap();
    assert_eq!(report.excluded_exact_pairs, 1);
    assert!(report.pairs.is_empty());
    assert_eq!(report.compared_pairs, 0);
}

#[test]
fn similarity_alone_excludes_actual_byte_identical_files() {
    let fixture = Fixture::new();
    fixture.put("report-one.txt", b"one two three");
    fixture.put("report-copy.txt", b"one two three");
    let result = scan(
        fixture.path(),
        &ScanOptions::default(),
        &AtomicBool::new(false),
        None,
    )
    .unwrap();
    let report = find_similar(
        &result,
        &SimilarOptions::default(),
        &AtomicBool::new(false),
        None,
    )
    .unwrap();
    assert_eq!(report.excluded_exact_pairs, 1);
    assert!(report.pairs.is_empty());
}

#[test]
fn changed_file_invalidates_cached_proof_even_when_size_and_modified_time_are_restored() {
    let fixture = Fixture::new();
    fixture.put("a.txt", b"abcd");
    fixture.put("b.txt", b"abcd");
    let result = scan(
        fixture.path(),
        &ScanOptions::default(),
        &AtomicBool::new(false),
        None,
    )
    .unwrap();
    let groups = find_duplicates(&result, &AtomicBool::new(false), None).unwrap();
    let path = &groups[0].paths[1];
    let original_time = fs::metadata(path).unwrap().modified().unwrap();
    std::thread::sleep(Duration::from_millis(2));
    fs::write(path, b"xxxx").unwrap();
    let file = OpenOptions::new().write(true).open(path).unwrap();
    file.set_times(fs::FileTimes::new().set_modified(original_time))
        .unwrap();
    assert!(validate_group(&groups[0]).is_err());
    assert_eq!(fs::read(path).unwrap(), b"xxxx");
}

#[test]
fn hardlink_names_do_not_count_as_independent_duplicates() {
    let fixture = Fixture::new();
    fixture.put("a.txt", b"one inode");
    fs::hard_link(
        fixture.path().join("a.txt"),
        fixture.path().join("alias.txt"),
    )
    .unwrap();
    let result = scan(
        fixture.path(),
        &ScanOptions::default(),
        &AtomicBool::new(false),
        None,
    )
    .unwrap();
    assert!(find_duplicates(&result, &AtomicBool::new(false), None)
        .unwrap()
        .is_empty());
    fixture.put("copy.txt", b"one inode");
    let result = scan(
        fixture.path(),
        &ScanOptions::default(),
        &AtomicBool::new(false),
        None,
    )
    .unwrap();
    let groups = find_duplicates(&result, &AtomicBool::new(false), None).unwrap();
    assert_eq!(groups.len(), 1);
    assert_eq!(groups[0].paths.len(), 2);
}

#[test]
fn stale_scan_is_not_silently_reused_or_rebuilt() {
    let fixture = Fixture::new();
    fixture.put("a.txt", b"same");
    fixture.put("b.txt", b"same");
    let result = scan(
        fixture.path(),
        &ScanOptions::default(),
        &AtomicBool::new(false),
        None,
    )
    .unwrap();
    fs::write(fixture.path().join("a.txt"), b"different length").unwrap();
    assert!(find_duplicates(&result, &AtomicBool::new(false), None).is_err());
}

#[test]
fn cancellation_and_unsafe_exclusion_patterns_fail_closed() {
    let fixture = Fixture::new();
    fixture.put("one.txt", b"one");
    let cancelled = scan(
        fixture.path(),
        &ScanOptions::default(),
        &AtomicBool::new(true),
        None,
    )
    .unwrap();
    assert!(cancelled.cancelled);
    assert!(find_duplicates(&cancelled, &AtomicBool::new(false), None).is_err());
    let options = ScanOptions {
        exclude_patterns: vec!["../private".into()],
        ..Default::default()
    };
    assert!(scan(fixture.path(), &options, &AtomicBool::new(false), None).is_err());
}

#[test]
fn wildcard_exclusions_and_size_filter_prune_before_inventory() {
    let fixture = Fixture::new();
    fixture.put("cache/a.txt", b"large");
    fixture.put("keep/one.tmp", b"large");
    fixture.put("keep/two.txt", b"large");
    fixture.put("keep/empty.txt", b"");
    let options = ScanOptions {
        exclude_patterns: vec!["CACHE".into(), "*.tmp".into()],
        min_size: 1,
        ..Default::default()
    };
    let result = scan(fixture.path(), &options, &AtomicBool::new(false), None).unwrap();
    assert_eq!(result.files.len(), 1);
    assert_eq!(result.files[0].name, "two.txt");
}

#[test]
fn text_similarity_is_a_candidate_even_when_its_score_is_one() {
    let fixture = Fixture::new();
    fixture.put("morning.txt", b"One two THREE");
    fixture.put("evening.txt", b"three, TWO one");
    let result = scan(
        fixture.path(),
        &ScanOptions::default(),
        &AtomicBool::new(false),
        None,
    )
    .unwrap();
    assert!(find_duplicates(&result, &AtomicBool::new(false), None)
        .unwrap()
        .is_empty());
    let similar = find_similar(
        &result,
        &SimilarOptions {
            compare_names: false,
            ..Default::default()
        },
        &AtomicBool::new(false),
        None,
    )
    .unwrap();
    assert!(similar.complete);
    assert_eq!(similar.pairs.len(), 1);
    assert_eq!(similar.pairs[0].text_score, Some(1.0));
    assert!(similar.pairs[0].image_score.is_none());
    assert!(similar
        .warnings
        .iter()
        .any(|warning| warning.contains("100%")));
}

#[test]
fn indexed_comparison_is_bounded_and_samples_beyond_the_inventory_start() {
    let fixture = Fixture::new();
    for index in 0..600 {
        fixture.put(
            &format!("report-{index:04}.txt"),
            format!("unique readonly content {index}").as_bytes(),
        );
    }
    let result = scan(
        fixture.path(),
        &ScanOptions::default(),
        &AtomicBool::new(false),
        None,
    )
    .unwrap();
    let options = SimilarOptions {
        max_pairs: 32,
        name_threshold: 0.0,
        compare_text: false,
        ..Default::default()
    };
    let report = find_similar(&result, &options, &AtomicBool::new(false), None).unwrap();
    assert!(!report.complete);
    assert_eq!(report.possible_pairs, 179_700);
    assert_eq!(report.compared_pairs, 32);
    assert!(report.pairs.iter().any(|pair| result
        .files
        .iter()
        .position(|file| file.path == pair.right)
        .unwrap()
        > 400));
}

#[test]
fn image_evidence_works_across_formats_without_treating_it_as_exact_bytes() {
    let fixture = Fixture::new();
    let image = image::RgbImage::from_fn(80, 60, |x, y| {
        image::Rgb([(x * 3) as u8, (y * 4) as u8, 120])
    });
    image.save(fixture.path().join("photo.png")).unwrap();
    image
        .save(fixture.path().join("different-title.jpg"))
        .unwrap();
    let result = scan(
        fixture.path(),
        &ScanOptions::default(),
        &AtomicBool::new(false),
        None,
    )
    .unwrap();
    assert!(find_duplicates(&result, &AtomicBool::new(false), None)
        .unwrap()
        .is_empty());
    let options = SimilarOptions {
        compare_names: false,
        compare_text: false,
        ..Default::default()
    };
    let report = find_similar(&result, &options, &AtomicBool::new(false), None).unwrap();
    assert_eq!(report.pairs.len(), 1);
    assert!(report.pairs[0].image_score.unwrap() > 0.90);
    assert!(report.pairs[0].text_score.is_none());
}

#[test]
fn solid_black_and_white_images_do_not_match_from_a_constant_hash() {
    let fixture = Fixture::new();
    image::RgbImage::from_pixel(30, 30, image::Rgb([0_u8, 0, 0]))
        .save(fixture.path().join("black.png"))
        .unwrap();
    image::RgbImage::from_pixel(30, 30, image::Rgb([255_u8, 255, 255]))
        .save(fixture.path().join("white.png"))
        .unwrap();
    let result = scan(
        fixture.path(),
        &ScanOptions::default(),
        &AtomicBool::new(false),
        None,
    )
    .unwrap();
    let options = SimilarOptions {
        compare_names: false,
        compare_text: false,
        ..Default::default()
    };
    assert!(
        find_similar(&result, &options, &AtomicBool::new(false), None)
            .unwrap()
            .pairs
            .is_empty()
    );
}

#[cfg(unix)]
#[test]
fn symbolic_files_and_directories_are_never_followed() {
    use std::os::unix::fs::symlink;
    let fixture = Fixture::new();
    fixture.put("real/one.txt", b"one");
    symlink(
        fixture.path().join("real"),
        fixture.path().join("link-folder"),
    )
    .unwrap();
    symlink(
        fixture.path().join("real/one.txt"),
        fixture.path().join("link-file.txt"),
    )
    .unwrap();
    let result = scan(
        fixture.path(),
        &ScanOptions::default(),
        &AtomicBool::new(false),
        None,
    )
    .unwrap();
    assert_eq!(result.files.len(), 1);
    assert_eq!(result.warnings.len(), 2);
    assert!(fingerprint(&fixture.path().join("link-file.txt")).is_err());
}
