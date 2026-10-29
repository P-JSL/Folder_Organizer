//! New common/native report contracts; no prior report tests are rerun.
use folder_organizer_rust::{
    core::{self, ScanOptions},
    reporting,
    similarity::{CandidateIndexMetrics, SimilarAnalysis, SimilarOptions},
};
use serde_json::Value;
use std::{fs, path::PathBuf, sync::atomic::AtomicBool};

struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        let mut random = [0_u8; 12];
        getrandom::getrandom(&mut random).unwrap();
        let id: String = random.iter().map(|n| format!("{n:02x}")).collect();
        let path = std::env::temp_dir().join(format!("organizer-report-v070-{id}"));
        fs::create_dir(&path).unwrap();
        Self(path)
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

#[test]
fn common_unrequested_containers_and_similar_only_request_semantics_match_the_shared_contract() {
    let fixture = Fixture::new();
    fixture.put("a.TXT", b"same original bytes");
    fixture.put("b.TXT", b"same original bytes");
    let scan = fixture.scan();
    let empty = reporting::common_report(&scan, &ScanOptions::default(), None, &None).unwrap();
    assert_eq!(empty["action_authority"], false);
    assert_eq!(empty["inventory"]["files"][0]["extension"], ".txt");
    assert!(empty["duplicates"]["metrics"]
        .as_object()
        .unwrap()
        .is_empty());
    assert!(empty["similarity"]["thresholds"]
        .as_object()
        .unwrap()
        .is_empty());
    assert!(empty["similarity"]["warnings"]
        .as_array()
        .unwrap()
        .is_empty());
    assert!(empty["similarity"]["candidate_index"]
        .as_object()
        .unwrap()
        .is_empty());
    let output = fixture.0.join("similar-common.json");
    let code = folder_organizer_rust::cli::run(vec![
        fixture.0.to_string_lossy().into_owned(),
        "--similar".into(),
        "--similar-method".into(),
        "name".into(),
        "--json-common".into(),
        output.to_string_lossy().into_owned(),
    ])
    .unwrap();
    assert_eq!(code, 0);
    let report: Value = serde_json::from_slice(&fs::read(output).unwrap()).unwrap();
    assert_eq!(report["duplicates"]["requested"], false);
    assert!(report["duplicates"]["complete"].is_null());
    assert!(report["duplicates"]["groups"]
        .as_array()
        .unwrap()
        .is_empty());
    assert!(report["duplicates"]["metrics"]
        .as_object()
        .unwrap()
        .is_empty());
    assert!(report["similarity"]["pairs"].as_array().unwrap().is_empty());
}

#[test]
fn common_json_preserves_signed_nanoseconds_above_javascript_integer_precision_and_null_birthtime()
{
    let fixture = Fixture::new();
    fixture.put("with-birthtime.txt", b"metadata only");
    fixture.put("without-birthtime.txt", b"separate metadata");
    let mut scan = fixture.scan();
    scan.files.sort_by(|a, b| a.name.cmp(&b.name));
    scan.files[0].modified_ns = 9_007_199_254_740_993;
    scan.files[0].created_ns = Some(-9_007_199_254_740_995);
    scan.files[1].modified_ns = -1_234_567_891;
    scan.files[1].created_ns = None;
    let report = reporting::common_report(&scan, &ScanOptions::default(), None, &None).unwrap();
    assert_eq!(
        report["inventory"]["files"][0]["modified_ns"],
        "9007199254740993"
    );
    assert_eq!(
        report["inventory"]["files"][0]["created_ns"],
        "-9007199254740995"
    );
    assert_eq!(
        report["inventory"]["files"][1]["modified_ns"],
        "-1234567891"
    );
    assert!(report["inventory"]["files"][1]["created_ns"].is_null());
    let encoded = serde_json::to_string(&report).unwrap();
    let roundtrip: Value = serde_json::from_str(&encoded).unwrap();
    assert_eq!(roundtrip, report);
    let native = fixture.0.join("native.json");
    reporting::export_analysis_json(&scan, &ScanOptions::default(), None, &None, &native).unwrap();
    let native: Value = serde_json::from_slice(&fs::read(native).unwrap()).unwrap();
    assert_eq!(
        native["scan"]["files"][0]["modified_ns"].as_i64(),
        Some(9_007_199_254_740_993)
    );
    assert_eq!(
        native["scan"]["files"][0]["created_ns"].as_i64(),
        Some(-9_007_199_254_740_995)
    );
    assert!(native["scan"]["files"][1]["created_ns"].is_null());
}

#[test]
fn common_csv_has_stable_columns_escapes_formula_text_and_never_replaces_an_existing_report() {
    let fixture = Fixture::new();
    fixture.put("=SUM(1+1).@cmd", b"source bytes stay private");
    let mut scan = fixture.scan();
    scan.files[0].modified_ns = -1_234_567_891;
    scan.files[0].created_ns = None;
    let path = fixture.0.join("inventory.csv");
    reporting::export_common_csv(&scan, &path).unwrap();
    let original = fs::read(&path).unwrap();
    let mut reader = csv::Reader::from_reader(original.as_slice());
    assert_eq!(
        reader.headers().unwrap().iter().collect::<Vec<_>>(),
        [
            "relative_path",
            "size",
            "modified_ns",
            "created_ns",
            "kind",
            "extension"
        ]
    );
    let records = reader.records().collect::<Result<Vec<_>, _>>().unwrap();
    assert_eq!(records.len(), 1);
    assert_eq!(&records[0][0], "'=SUM(1+1).@cmd");
    assert_eq!(&records[0][2], "-1234567891");
    assert_eq!(&records[0][3], "");
    // Native inventory extensions include their leading dot, so this value is
    // already ordinary text while the leading '=' path must be neutralized.
    assert_eq!(&records[0][5], ".@cmd");
    scan.files[0].size = 777;
    assert!(reporting::export_common_csv(&scan, &path).is_err());
    assert_eq!(fs::read(&path).unwrap(), original);
    assert!(!String::from_utf8(original)
        .unwrap()
        .contains("source bytes stay private"));
}

#[test]
fn stale_candidate_remains_visible_as_partial_in_common_native_and_html_without_content_or_authority(
) {
    let fixture = Fixture::new();
    let secret = b"NEVER INCLUDE THIS PRIVATE DOCUMENT TEXT";
    fixture.put("a.txt", secret);
    fixture.put("b.txt", secret);
    let gone = fixture.put("gone.txt", secret);
    let scan = fixture.scan();
    fs::remove_file(gone).unwrap();
    let mut analysis = core::analyze_duplicates(&scan, &AtomicBool::new(false), None).unwrap();
    assert_eq!(analysis.groups.len(), 1);
    assert!(!analysis.complete);
    analysis.warnings[0]
        .message
        .push_str(" <script>alert('untrusted error')</script>");
    let common =
        reporting::common_report(&scan, &ScanOptions::default(), Some(&analysis), &None).unwrap();
    assert_eq!(common["duplicates"]["complete"], false);
    assert_eq!(common["duplicates"]["warning_count"], 1);
    assert_eq!(common["duplicates"]["warnings"][0]["path"], "gone.txt");
    assert_eq!(
        common["duplicates"]["groups"][0]["paths"]
            .as_array()
            .unwrap()
            .len(),
        2
    );
    assert_eq!(common["duplicates"]["metrics"]["skipped_files"], 1);
    let native_path = fixture.0.join("native.json");
    reporting::export_analysis_json(
        &scan,
        &ScanOptions::default(),
        Some(&analysis),
        &None,
        &native_path,
    )
    .unwrap();
    let native_bytes = fs::read(native_path).unwrap();
    let native: Value = serde_json::from_slice(&native_bytes).unwrap();
    assert_eq!(native["duplicate_analysis"]["complete"], false);
    assert_eq!(native["duplicate_analysis"]["warning_count"], 1);
    assert_eq!(native["duplicates"].as_array().unwrap().len(), 1);
    let html_path = fixture.0.join("partial.html");
    reporting::export_analysis_html(
        &scan,
        &ScanOptions::default(),
        Some(&analysis),
        &None,
        &html_path,
    )
    .unwrap();
    let html = fs::read_to_string(html_path).unwrap();
    assert!(html.contains("부분 검사: true"));
    assert!(html.contains("gone.txt"));
    assert!(html.contains("&lt;script&gt;"));
    assert!(!html.contains("<script>"));
    for payload in [
        String::from_utf8(native_bytes).unwrap(),
        serde_json::to_string(&common).unwrap(),
        html,
    ] {
        assert!(!payload.contains(std::str::from_utf8(secret).unwrap()));
        assert!(!payload.contains("\"proof\""));
        assert!(!payload.contains("\"snapshots\""));
    }
    core::validate_group(&analysis.groups[0]).unwrap();
}

#[test]
fn bounded_or_cancelled_similarity_marks_the_html_summary_partial_even_when_inventory_is_complete()
{
    let fixture = Fixture::new();
    fixture.put("sample.txt", b"private text");
    let scan = fixture.scan();
    for cancelled in [false, true] {
        let similar = Some(SimilarAnalysis {
            pairs: vec![],
            compared_pairs: 0,
            possible_pairs: 100,
            excluded_exact_pairs: 0,
            complete: false,
            cancelled,
            warnings: vec!["This is a bounded or cancelled analysis".into()],
            options: SimilarOptions::default(),
            candidate_index: CandidateIndexMetrics {
                enabled: true,
                ..Default::default()
            },
            feature_failures: 0,
        });
        let report =
            reporting::common_report(&scan, &ScanOptions::default(), None, &similar).unwrap();
        assert_eq!(report["inventory"]["complete"], true);
        assert_eq!(report["similarity"]["complete"], false);
        assert_eq!(report["similarity"]["approximate"], true);
        assert_eq!(report["duplicates"]["requested"], false);
        assert!(report["duplicates"]["complete"].is_null());
        let html_path = fixture.0.join(format!("similar-{cancelled}.html"));
        reporting::export_analysis_html(&scan, &ScanOptions::default(), None, &similar, &html_path)
            .unwrap();
        let html = fs::read_to_string(html_path).unwrap();
        assert!(html.contains("부분 검사: true"));
        assert!(html.contains("bounded or cancelled analysis"));
        assert!(!html.contains("private text"));
    }
}
