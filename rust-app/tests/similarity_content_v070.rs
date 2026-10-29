//! Report-review corpus for new bounded content candidate retrieval.
use folder_organizer_rust::{
    core::{self, ScanOptions, ScanResult},
    similarity::{self, SimilarAnalysis, SimilarOptions},
};
use std::{
    collections::BTreeSet,
    fs::{self, FileTimes, OpenOptions},
    path::{Path, PathBuf},
    sync::atomic::{AtomicBool, Ordering},
};

struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        let mut random = [0_u8; 12];
        getrandom::getrandom(&mut random).unwrap();
        let id: String = random.iter().map(|value| format!("{value:02x}")).collect();
        let path = std::env::temp_dir().join(format!("organizer-similarity-v070-{id}"));
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
        scan.files
            .sort_by(|a, b| a.relative_path.cmp(&b.relative_path));
        scan
    }
    fn decoys(&self, count: usize) {
        for id in 0..count {
            self.write(
                &format!("noise-{id:04}.txt"),
                &format!("noise{id} isolated{id} unrelated{id} item{id}"),
            );
        }
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn members(left: &Path, right: &Path) -> BTreeSet<String> {
    [left, right]
        .into_iter()
        .map(|path| path.file_name().unwrap().to_string_lossy().into_owned())
        .collect()
}

fn assert_index_bounds(report: &SimilarAnalysis, files: usize, budget: usize) {
    let index = &report.candidate_index;
    assert!(index.enabled && !report.complete);
    assert_eq!(index.files_visited, files);
    assert!(report.compared_pairs <= budget);
    assert!(index.candidate_pairs_selected <= budget);
    assert!(index.candidate_pairs_generated <= files * index.candidates_per_file);
    assert!(index.postings_scanned <= index.candidate_lookups * index.posting_limit);
    assert!(index.index_keys_retained_peak <= index.index_key_limit);
}

#[test]
fn labeled_text_corpus_retrieves_distant_unrelated_titles_across_formats_and_cache_eviction() {
    let fixture = Fixture::new();
    fixture.decoys(280);
    fixture.write("000-amber.txt", "amber birch cedar delta elm fern ginkgo hazel iris jasmine kelp larch maple nettle oak pine quince reed spruce thyme");
    fixture.write("zzz-월간보고서.md", "thyme spruce reed quince pine oak nettle maple larch kelp jasmine iris hazel ginkgo fern elm delta cedar birch amber updated");
    let scan = fixture.scan();
    let options = SimilarOptions {
        compare_names: false,
        compare_images: false,
        max_pairs: 32,
        ..Default::default()
    };
    let report = similarity::find_similar(&scan, &options, &AtomicBool::new(false), None).unwrap();
    assert_index_bounds(&report, 282, 32);
    assert_eq!(report.candidate_index.content_files_indexed, 282);
    assert_eq!(report.feature_failures, 0);
    assert_eq!(
        report.pairs.len(),
        1,
        "The labeled fixture has exactly one positive pair"
    );
    let pair = &report.pairs[0];
    assert_eq!(
        members(&pair.left, &pair.right),
        BTreeSet::from(["000-amber.txt".into(), "zzz-월간보고서.md".into()])
    );
    assert!(pair.text_score.unwrap() > 0.97);
    assert_eq!(pair.name_score, 0.0);
    assert!(pair.image_score.is_none());
    assert_eq!(report.options.max_pairs, 32);
    // One labeled positive retrieved; no unlabeled pair returned: corpus
    // precision and recall are both 1.0, without claiming universal recall.
    assert!(!fixture.0.join("delete_files").exists());
}

#[test]
fn labeled_image_corpus_retrieves_far_cross_format_pair_without_name_candidates() {
    let fixture = Fixture::new();
    let image = image::RgbImage::from_fn(80, 60, |x, y| {
        image::Rgb([(x * 3) as u8, (y * 4) as u8, 120])
    });
    image.save(fixture.0.join("000-landscape.png")).unwrap();
    for id in 0..70_u32 {
        let noise = image::RgbImage::from_fn(40, 30, |x, y| {
            // Labeled negatives have visibly distinct coarse structures. Pure
            // pixel noise collapses towards grey when reduced to 8x8, so it
            // cannot serve as a guaranteed negative for this bounded feature.
            let mut bits = (x / 5 + 1).wrapping_mul(73_856_093)
                ^ (y / 5 + 1).wrapping_mul(19_349_663)
                ^ (id + 1).wrapping_mul(83_492_791);
            bits ^= bits >> 13;
            bits = bits.wrapping_mul(0x85eb_ca6b);
            bits ^= bits >> 16;
            image::Rgb([bits as u8, (bits >> 8) as u8, (bits >> 16) as u8])
        });
        noise
            .save(fixture.0.join(format!("noise-{id:04}.png")))
            .unwrap();
    }
    image.save(fixture.0.join("zzz-다른제목.jpg")).unwrap();
    let scan = fixture.scan();
    let options = SimilarOptions {
        compare_names: false,
        compare_text: false,
        image_threshold: 0.96,
        max_pairs: 16,
        ..Default::default()
    };
    let report = similarity::find_similar(&scan, &options, &AtomicBool::new(false), None).unwrap();
    assert_index_bounds(&report, 72, 16);
    assert_eq!(report.candidate_index.content_files_indexed, 72);
    assert_eq!(report.feature_failures, 0);
    assert_eq!(
        report.pairs.len(),
        1,
        "The labeled fixture has one cross-format image pair"
    );
    let pair = &report.pairs[0];
    assert_eq!(
        members(&pair.left, &pair.right),
        BTreeSet::from(["000-landscape.png".into(), "zzz-다른제목.jpg".into()])
    );
    assert!(pair.image_score.unwrap() >= 0.96);
    assert_eq!(pair.name_score, 0.0);
    assert!(pair.text_score.is_none());
}

#[test]
fn high_frequency_feature_collisions_are_review_candidates_without_exact_or_delete_authority() {
    let fixture = Fixture::new();
    let image = image::RgbImage::from_fn(80, 60, |x, y| {
        image::Rgb([(x * 3) as u8, (y * 4) as u8, 120])
    });
    image.save(fixture.0.join("000-landscape.png")).unwrap();
    for id in 0..70_u32 {
        // Preserve the original ambiguous corpus observed during review. Pixel
        // noise reduces towards similar tiny grey images: scoring cannot infer
        // semantic identity from those deliberately bounded features.
        let noise = image::RgbImage::from_fn(40, 30, |x, y| {
            let mut bits = (x + 1).wrapping_mul(73_856_093)
                ^ (y + 1).wrapping_mul(19_349_663)
                ^ (id + 1).wrapping_mul(83_492_791);
            bits ^= bits >> 13;
            image::Rgb([bits as u8, (bits >> 8) as u8, (bits >> 16) as u8])
        });
        noise
            .save(fixture.0.join(format!("noise-{id:04}.png")))
            .unwrap();
    }
    image.save(fixture.0.join("zzz-다른제목.jpg")).unwrap();
    let scan = fixture.scan();
    let before: Vec<_> = scan
        .files
        .iter()
        .map(|entry| fs::read(&entry.path).unwrap())
        .collect();
    let cancel = AtomicBool::new(false);
    let exact = core::analyze_duplicates(&scan, &cancel, None).unwrap();
    assert!(exact.groups.is_empty());
    let options = SimilarOptions {
        compare_names: false,
        compare_text: false,
        image_threshold: 0.96,
        max_pairs: 16,
        ..Default::default()
    };
    let report =
        similarity::find_similar_excluding(&scan, &options, &exact.groups, &cancel, None).unwrap();
    let ambiguous = report
        .pairs
        .iter()
        .filter(|pair| {
            pair.left
                .file_name()
                .unwrap()
                .to_string_lossy()
                .starts_with("noise-")
                || pair
                    .right
                    .file_name()
                    .unwrap()
                    .to_string_lossy()
                    .starts_with("noise-")
        })
        .count();
    assert!(
        ambiguous > 0,
        "Retain the observed high-frequency feature ambiguity"
    );
    assert!(report
        .pairs
        .iter()
        .all(|pair| pair.reason.contains("삭제 근거 아님")));
    assert!(report
        .warnings
        .iter()
        .any(|warning| warning.contains("고주파")));
    assert_index_bounds(&report, 72, 16);
    assert!(scan
        .files
        .iter()
        .zip(before)
        .all(|(entry, bytes)| fs::read(&entry.path).unwrap() == bytes));
    assert!(!fixture.0.join("delete_files").exists());
    println!("High-frequency review corpus: {} selected pairs, {} returned candidates, {ambiguous} noise-feature ambiguities; zero exact groups and unchanged source bytes.", report.compared_pairs, report.pairs.len());
}

#[test]
fn changed_text_after_content_index_cannot_reuse_cached_features_even_with_restored_mtime() {
    let fixture = Fixture::new();
    fixture.decoys(70);
    let left = fixture.write("000-left.txt", "alpha beta gamma delta epsilon");
    fixture.write("zzz-right.md", "epsilon delta gamma beta alpha!");
    let modified = fs::metadata(&left).unwrap().modified().unwrap();
    let scan = fixture.scan();
    let options = SimilarOptions {
        compare_names: false,
        compare_images: false,
        max_pairs: 16,
        ..Default::default()
    };
    let callback = |progress: core::Progress| {
        if progress.phase == "similar_index" && progress.completed == scan.files.len() as u64 {
            fs::write(&left, "OMEGA BETA GAMMA DELTA EPSILON").unwrap();
            OpenOptions::new()
                .write(true)
                .open(&left)
                .unwrap()
                .set_times(FileTimes::new().set_modified(modified))
                .unwrap();
        }
    };
    let report =
        similarity::find_similar(&scan, &options, &AtomicBool::new(false), Some(&callback))
            .unwrap();
    assert!(report.pairs.is_empty());
    assert!(report.feature_failures > 0);
    assert!(!report.complete);
    assert_eq!(
        fs::read_to_string(&left).unwrap(),
        "OMEGA BETA GAMMA DELTA EPSILON"
    );
}

#[test]
fn cancel_during_content_index_returns_no_stale_partial_suggestions() {
    let fixture = Fixture::new();
    fixture.decoys(70);
    let scan = fixture.scan();
    let options = SimilarOptions {
        compare_names: false,
        compare_images: false,
        max_pairs: 1,
        ..Default::default()
    };
    let cancel = AtomicBool::new(false);
    let callback = |progress: core::Progress| {
        if progress.phase == "similar_index" && progress.completed == 1 {
            cancel.store(true, Ordering::Relaxed);
        }
    };
    let report = similarity::find_similar(&scan, &options, &cancel, Some(&callback)).unwrap();
    assert!(report.cancelled && !report.complete);
    assert_eq!(report.candidate_index.files_visited, 1);
    assert_eq!(report.compared_pairs, 0);
    assert!(report.pairs.is_empty());
}

#[test]
fn content_score_one_remains_similar_and_authenticated_exact_pair_is_excluded() {
    let fixture = Fixture::new();
    fixture.write("first.txt", "alpha beta gamma");
    fixture.write("copy.txt", "alpha beta gamma");
    fixture.write("reordered.md", "gamma, beta alpha");
    let scan = fixture.scan();
    let cancel = AtomicBool::new(false);
    let exact = core::analyze_duplicates(&scan, &cancel, None).unwrap();
    assert_eq!(exact.groups.len(), 1);
    let options = SimilarOptions {
        compare_names: false,
        compare_images: false,
        ..Default::default()
    };
    let report =
        similarity::find_similar_excluding(&scan, &options, &exact.groups, &cancel, None).unwrap();
    assert!(report.complete);
    assert_eq!(report.excluded_exact_pairs, 1);
    assert_eq!(report.pairs.len(), 2);
    assert!(report.pairs.iter().all(|pair| pair.text_score == Some(1.0)
        && (pair.left.ends_with("reordered.md") || pair.right.ends_with("reordered.md"))));
    assert_eq!(
        fs::read_to_string(fixture.0.join("first.txt")).unwrap(),
        "alpha beta gamma"
    );
    assert!(!fixture.0.join("delete_files").exists());
}
