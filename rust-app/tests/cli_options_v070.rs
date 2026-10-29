//! New report-review CLI regressions; previously passed suites are not rerun.
use folder_organizer_rust::cli;
use serde_json::Value;
use std::{
    fs,
    path::{Path, PathBuf},
};

struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        let mut random = [0_u8; 12];
        getrandom::getrandom(&mut random).unwrap();
        let id: String = random.iter().map(|byte| format!("{byte:02x}")).collect();
        let path = std::env::temp_dir().join(format!("organizer-cli-options-v070-{id}"));
        fs::create_dir(&path).unwrap();
        fs::create_dir(path.join("input")).unwrap();
        Self(path)
    }
    fn input(&self) -> PathBuf {
        self.0.join("input")
    }
    fn write(&self, name: &str, body: &str) {
        fs::write(self.input().join(name), body.as_bytes()).unwrap();
    }
    fn report(&self, name: &str, options: &[&str]) -> Value {
        let output = self.0.join(name);
        let mut arguments = options
            .iter()
            .map(|value| (*value).to_owned())
            .collect::<Vec<_>>();
        arguments.extend([
            self.input().to_string_lossy().into_owned(),
            "--similar".into(),
            "--json".into(),
            output.to_string_lossy().into_owned(),
        ]);
        assert_eq!(cli::run(arguments).unwrap(), 0);
        serde_json::from_slice(&fs::read(output).unwrap()).unwrap()
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[test]
fn individual_similarity_options_reject_invalid_values_before_opening_a_folder() {
    for option in ["--name-threshold", "--text-threshold", "--image-threshold"] {
        for value in [
            "NaN",
            "inf",
            "-inf",
            "0.49",
            "1.01",
            "0.49999999999",
            "1.00000000001",
            "not-a-number",
            "",
        ] {
            let error = cli::run(vec![
                "a-folder-that-does-not-exist-v070".into(),
                "--similar".into(),
                option.into(),
                value.into(),
            ])
            .unwrap_err();
            assert!(error.contains(option) && error.contains("0.5~1"), "{error}");
        }
    }
    for value in ["0", "100001", "-1", "1.5", "NaN", "184467440737095516160"] {
        let error = cli::run(vec![
            "a-folder-that-does-not-exist-v070".into(),
            "--max-pairs".into(),
            value.into(),
        ])
        .unwrap_err();
        assert!(error.contains("--max-pairs"), "{error}");
    }
    let error = cli::run(vec![
        "a-folder-that-does-not-exist-v070".into(),
        "--similar-method".into(),
        "semantic".into(),
    ])
    .unwrap_err();
    assert!(error.contains("--similar-method"), "{error}");
}

#[test]
fn common_export_options_write_both_engine_neutral_files_in_one_analysis() {
    let fixture = Fixture::new();
    fixture.write("first.txt", "identical ordinary bytes");
    fixture.write("second.txt", "identical ordinary bytes");
    let json = fixture.0.join("common.json");
    let csv = fixture.0.join("common.csv");
    let result = cli::run(vec![
        "--duplicates".into(),
        format!("--json-common={}", json.display()),
        "--csv-common".into(),
        csv.to_string_lossy().into_owned(),
        fixture.input().to_string_lossy().into_owned(),
    ])
    .unwrap();
    assert_eq!(result, 0);
    let report: Value = serde_json::from_slice(&fs::read(json).unwrap()).unwrap();
    assert_eq!(report["inventory"]["files"].as_array().unwrap().len(), 2);
    assert_eq!(report["duplicates"]["groups"].as_array().unwrap().len(), 1);
    let csv = fs::read_to_string(csv).unwrap();
    assert!(csv.contains("first.txt") && csv.contains("second.txt"));
    assert_eq!(
        fs::read_to_string(fixture.input().join("first.txt")).unwrap(),
        "identical ordinary bytes"
    );
    assert_eq!(
        fs::read_to_string(fixture.input().join("second.txt")).unwrap(),
        "identical ordinary bytes"
    );
}

#[test]
fn missing_similarity_option_values_produce_specific_diagnostics() {
    for option in [
        "--similar-method",
        "--name-threshold",
        "--text-threshold",
        "--image-threshold",
        "--max-pairs",
    ] {
        let error = cli::run(vec![
            "a-folder-that-does-not-exist-v070".into(),
            option.into(),
        ])
        .unwrap_err();
        assert!(
            error.contains(option) && error.contains("값이 필요"),
            "{error}"
        );
    }
}

#[test]
fn explicit_methods_and_repeated_method_union_reach_the_scoring_engine() {
    let fixture = Fixture::new();
    fixture.write("untitled-left.txt", "One two THREE");
    fixture.write("untitled-right.txt", "three, TWO one");
    let text_only = fixture.report(
        "text.json",
        &["--similar-method=text", "--text-threshold", "1"],
    );
    let pairs = text_only["similar"]["pairs"].as_array().unwrap();
    assert_eq!(pairs.len(), 1);
    assert_eq!(pairs[0]["name_score"], 0.0);
    assert_eq!(pairs[0]["text_score"], 1.0);
    assert!(pairs[0]["image_score"].is_null());
    assert_eq!(text_only["similar"]["options"]["compare_names"], false);
    assert_eq!(text_only["similar"]["options"]["compare_text"], true);
    assert_eq!(text_only["similar"]["options"]["compare_images"], false);
    assert!(text_only["duplicates"].as_array().unwrap().is_empty());
    let names_only = fixture.report(
        "names.json",
        &["--similar-method", "name", "--name-threshold=1"],
    );
    assert!(names_only["similar"]["pairs"]
        .as_array()
        .unwrap()
        .is_empty());
    let both = fixture.report(
        "both.json",
        &[
            "--similar-method",
            "name",
            "--similar-method",
            "text",
            "--name-threshold",
            "1",
            "--text-threshold=1",
        ],
    );
    let pair = &both["similar"]["pairs"][0];
    assert!(pair["name_score"].as_f64().unwrap() > 0.0);
    assert_eq!(pair["text_score"], 1.0);
    for name in ["untitled-left.txt", "untitled-right.txt"] {
        assert!(fixture.input().join(name).is_file());
    }
}

#[test]
fn text_threshold_and_pair_budget_change_the_report_without_source_writes() {
    let fixture = Fixture::new();
    fixture.write("first.txt", "alpha beta gamma");
    fixture.write("second.txt", "alpha beta gamma delta");
    fixture.write("third.txt", "alpha beta gamma epsilon zeta");
    let high = fixture.report(
        "high.json",
        &["--similar-method", "text", "--text-threshold", "1"],
    );
    assert!(high["similar"]["pairs"].as_array().unwrap().is_empty());
    let low = fixture.report(
        "low.json",
        &[
            "--similar-method",
            "text",
            "--text-threshold",
            "0.5",
            "--max-pairs=1",
            "--image-threshold=1",
        ],
    );
    assert_eq!(low["similar"]["compared_pairs"], 1);
    assert_eq!(low["similar"]["complete"], false);
    assert_eq!(low["similar"]["options"]["max_pairs"], 1);
    assert_eq!(low["similar"]["pairs"].as_array().unwrap().len(), 1);
    assert_eq!(
        fs::read_to_string(fixture.input().join("first.txt")).unwrap(),
        "alpha beta gamma"
    );
    assert_eq!(
        fs::read_to_string(fixture.input().join("second.txt")).unwrap(),
        "alpha beta gamma delta"
    );
    assert_eq!(
        fs::read_to_string(fixture.input().join("third.txt")).unwrap(),
        "alpha beta gamma epsilon zeta"
    );
    assert!(!Path::new(&fixture.input().join("delete_files")).exists());
}
