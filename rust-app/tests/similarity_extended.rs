//! Targeted regressions for bounded feature reuse added after the extended review.
use folder_organizer_rust::{
    core::{self, ScanOptions, ScanResult},
    similarity::{self, CandidateIndexMetrics, SimilarOptions},
};
use std::{
    collections::BTreeSet,
    fs::{self, FileTimes, OpenOptions},
    path::{Path, PathBuf},
    sync::atomic::{AtomicBool, Ordering},
    time::Instant,
};

struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        let mut random = [0_u8; 12];
        getrandom::getrandom(&mut random).unwrap();
        let id: String = random.iter().map(|byte| format!("{byte:02x}")).collect();
        let path = std::env::temp_dir().join(format!("organizer-feature-reuse-{id}"));
        fs::create_dir(&path).unwrap();
        Self(path)
    }

    fn write(&self, name: &str, body: &str) -> PathBuf {
        let path = self.0.join(name);
        fs::write(&path, body).unwrap();
        path
    }

    fn scan(&self) -> ScanResult {
        let mut scan = core::scan(
            &self.0,
            &ScanOptions::default(),
            &AtomicBool::new(false),
            None,
        )
        .unwrap();
        scan.files.sort_by(|a, b| a.path.cmp(&b.path));
        scan
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

// Independent reference for the established name-score contract. These strings
// intentionally retain the old human-readable bigrams rather than the packed
// representation used by the optimized engine.
fn reference_keys(path: &Path) -> BTreeSet<String> {
    let name = path.file_stem().unwrap().to_string_lossy().to_lowercase();
    let chars: Vec<_> = name
        .chars()
        .filter(|ch| ch.is_alphanumeric())
        .take(256)
        .collect();
    if chars.len() < 2 {
        return BTreeSet::from([format!("n:{}", chars.into_iter().collect::<String>())]);
    }
    let mut result = BTreeSet::new();
    for pair in chars.windows(2) {
        result.insert(format!("n:{}{}", pair[0], pair[1]));
        if result.len() == 64 {
            break;
        }
    }
    result
}

#[test]
fn exhaustive_unicode_name_scores_match_reference_with_one_feature_load_per_file() {
    let fixture = Fixture::new();
    for name in [
        "_.bin",
        "!.bin",
        "a.bin",
        "A.dat",
        "가.bin",
        "가가.bin",
        "𐐀.bin",
        "𐐨.dat",
        "𐐀가.bin",
        "𐐨가.dat",
        "ＡＢ.bin",
        "ａｂ.dat",
    ] {
        fixture.write(name, name);
    }
    for id in 0..28 {
        fixture.write(
            &format!("공동-월간보고서-ProjectAlpha-{id:02}.bin"),
            &format!("original {id}"),
        );
    }
    let scan = fixture.scan();
    let before: Vec<_> = scan
        .files
        .iter()
        .map(|entry| fs::read(&entry.path).unwrap())
        .collect();
    let options = SimilarOptions {
        compare_text: false,
        compare_images: false,
        name_threshold: 0.0,
        max_pairs: 1000,
        ..Default::default()
    };
    let start = Instant::now();
    let report =
        similarity::find_similar_excluding(&scan, &options, &[], &AtomicBool::new(false), None)
            .unwrap();
    let elapsed = start.elapsed();
    assert!(report.complete && !report.cancelled);
    assert_eq!(report.compared_pairs, 780);
    assert_eq!(report.pairs.len(), 780);
    for pair in &report.pairs {
        let left = reference_keys(&pair.left);
        let right = reference_keys(&pair.right);
        let score =
            2.0 * left.intersection(&right).count() as f32 / (left.len() + right.len()) as f32;
        assert_eq!(
            pair.name_score,
            score,
            "{} / {}",
            pair.left.display(),
            pair.right.display()
        );
        assert!(pair.text_score.is_none() && pair.image_score.is_none());
        assert!(pair.reason.contains("삭제 근거 아님"));
    }
    let metrics = &report.candidate_index;
    assert_eq!(metrics.name_feature_loads, 40);
    assert_eq!(metrics.name_feature_cache_hits, 1520);
    assert_eq!(metrics.name_feature_cache_evictions, 0);
    assert_eq!(metrics.name_feature_cache_peak, 40);
    assert_eq!(metrics.text_feature_loads, 0);
    assert!(scan
        .files
        .iter()
        .zip(before)
        .all(|(entry, bytes)| fs::read(&entry.path).unwrap() == bytes));
    println!("40 files / 780 pairs: legacy name construction calls=1560; observed loads={}, hits={}; full read-only analysis {:?}; scores unchanged.", metrics.name_feature_loads, metrics.name_feature_cache_hits, elapsed);
}

#[test]
fn unsupported_index_members_cannot_evict_supported_text_features() {
    let fixture = Fixture::new();
    fixture.write("000-first.txt", "amber birch cedar delta elm fern ginkgo hazel iris jasmine kelp larch maple nettle oak pine quince reed spruce thyme");
    fixture.write("001-second.md", "thyme spruce reed quince pine oak nettle maple larch kelp jasmine iris hazel ginkgo fern elm delta cedar birch amber updated");
    for id in 0..300 {
        fixture.write(
            &format!("common-project-{id:04}.bin"),
            &format!("binary profile {id}"),
        );
    }
    let scan = fixture.scan();
    let options = SimilarOptions {
        compare_images: false,
        max_pairs: 64,
        ..Default::default()
    };
    let report =
        similarity::find_similar_excluding(&scan, &options, &[], &AtomicBool::new(false), None)
            .unwrap();
    assert!(report.candidate_index.enabled && !report.complete);
    assert_eq!(report.feature_failures, 0);
    assert_eq!(report.candidate_index.files_visited, 302);
    assert_eq!(report.candidate_index.content_files_indexed, 2);
    assert_eq!(report.candidate_index.text_feature_loads, 2);
    assert_eq!(report.candidate_index.text_feature_cache_peak, 2);
    assert_eq!(report.candidate_index.text_feature_cache_evictions, 0);
    assert_eq!(report.candidate_index.text_feature_cache_hits, 2);
    assert_eq!(report.candidate_index.name_feature_loads, 302);
    assert_eq!(report.candidate_index.name_feature_cache_peak, 302);
    let pair = report
        .pairs
        .iter()
        .find(|pair| pair.text_score.is_some())
        .expect("supported content pair remains retrievable");
    assert!(pair.text_score.unwrap() > 0.97);
    assert_eq!(
        fs::read_to_string(fixture.0.join("common-project-0299.bin")).unwrap(),
        "binary profile 299"
    );
    assert!(!fixture.0.join("delete_files").exists());
    println!("302 mixed-format indexed files: supported text loads={}, retained peak={}, evictions={}; 300 unsupported extensions bypass text cache.", report.candidate_index.text_feature_loads, report.candidate_index.text_feature_cache_peak, report.candidate_index.text_feature_cache_evictions);
}

#[test]
fn name_cache_is_bounded_when_inventory_exceeds_its_budget() {
    let fixture = Fixture::new();
    for id in 0..4100 {
        fixture.write(
            &format!("common-bounded-project-{id:04}.dat"),
            "temporary input",
        );
    }
    let scan = fixture.scan();
    let options = SimilarOptions {
        compare_text: false,
        compare_images: false,
        max_pairs: 4,
        ..Default::default()
    };
    let report =
        similarity::find_similar_excluding(&scan, &options, &[], &AtomicBool::new(false), None)
            .unwrap();
    assert_eq!(report.candidate_index.files_visited, 4100);
    assert_eq!(report.compared_pairs, 4);
    assert_eq!(report.candidate_index.name_feature_cache_peak, 4096);
    assert!(report.candidate_index.name_feature_cache_evictions >= 4);
    assert!(report.candidate_index.name_feature_loads <= 4108);
    assert_eq!(report.candidate_index.text_feature_cache_peak, 0);
    assert_eq!(report.candidate_index.image_feature_cache_peak, 0);
}

#[test]
fn cached_names_do_not_hide_a_same_mtime_change_between_scored_pairs() {
    let fixture = Fixture::new();
    for id in 0..40 {
        fixture.write(&format!("common-report-{id:02}.dat"), "original file data");
    }
    let scan = fixture.scan();
    let victim = scan.files[39].path.clone();
    let modified = fs::metadata(&victim).unwrap().modified().unwrap();
    let changed = AtomicBool::new(false);
    let progress = |event: core::Progress| {
        if event.phase == "similarity"
            && event.completed == 64
            && !changed.swap(true, Ordering::Relaxed)
        {
            fs::write(&victim, "modified file data").unwrap();
            OpenOptions::new()
                .write(true)
                .open(&victim)
                .unwrap()
                .set_times(FileTimes::new().set_modified(modified))
                .unwrap();
        }
    };
    let options = SimilarOptions {
        compare_text: false,
        compare_images: false,
        name_threshold: 0.0,
        max_pairs: 1000,
        ..Default::default()
    };
    let report = similarity::find_similar_excluding(
        &scan,
        &options,
        &[],
        &AtomicBool::new(false),
        Some(&progress),
    )
    .unwrap();
    assert!(changed.load(Ordering::Relaxed));
    assert!(!report.complete);
    assert!(report.feature_failures > 0);
    assert!(report.candidate_index.name_feature_cache_hits > 0);
    assert_eq!(fs::read_to_string(victim).unwrap(), "modified file data");
}

#[test]
fn prior_candidate_metric_documents_remain_readable_after_additive_cache_metrics() {
    let old = r#"{"enabled":true,"files_visited":302,"content_files_indexed":2,"candidate_lookups":900,"postings_scanned":1700,"candidate_pairs_generated":1300,"candidate_pairs_selected":64,"index_keys_retained_peak":150,"index_evictions":0,"index_key_limit":65536,"posting_limit":32,"candidates_per_file":32}"#;
    let metrics: CandidateIndexMetrics = serde_json::from_str(old).unwrap();
    assert!(metrics.enabled);
    assert_eq!(metrics.files_visited, 302);
    assert_eq!(metrics.name_feature_loads, 0);
    assert_eq!(metrics.text_feature_cache_peak, 0);
    assert_eq!(metrics.image_feature_cache_evictions, 0);
}
