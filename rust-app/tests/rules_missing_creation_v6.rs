//! Missing provider birth time must skip only the rule that requires it.
use folder_organizer_rust::{
    core::{FileEntry, ScanMetrics, ScanResult},
    rules::{self, Rule},
};
use std::path::PathBuf;

fn scan() -> ScanResult {
    let root = if cfg!(windows) {
        PathBuf::from(r"C:\organizer-rule-fixture")
    } else {
        PathBuf::from("/organizer-rule-fixture")
    };
    ScanResult {
        files: vec![FileEntry {
            path: root.join("document.txt"),
            relative_path: "document.txt".into(),
            name: "document.txt".into(),
            size: 37,
            modified_ns: 1_700_000_000_000_000_000,
            created_ns: None,
            kind: "file".into(),
            extension: "txt".into(),
            identity: None,
        }],
        root,
        warnings: vec![],
        cancelled: false,
        metrics: ScanMetrics::default(),
    }
}

fn fallback() -> Rule {
    Rule {
        name: "Fallback".into(),
        target: "fallback/{ext}".into(),
        ..Rule::default()
    }
}

#[test]
fn missing_creation_date_condition_allows_later_eligible_rule() {
    let scan = scan();
    let first = Rule {
        name: "Creation date".into(),
        date_basis: "created".into(),
        start_date: Some("2020-01-01".into()),
        ..Rule::default()
    };
    let destination = scan.root.join("organized");
    let preview = rules::classification_preview(&scan, &[first, fallback()], &destination).unwrap();
    assert_eq!(preview.items.len(), 1);
    assert_eq!(preview.items[0].rule_name, "Fallback");
    assert_eq!(
        preview.items[0].target,
        destination.join("fallback/txt/document.txt")
    );
    assert_eq!(preview.warnings.len(), 1);
    assert!(preview.warnings[0].contains("생성일"));
}

#[test]
fn missing_creation_template_allows_later_eligible_rule_for_both_token_languages() {
    for target in ["{created:%Y/%m}", "{생성연도}/{생성월}"] {
        let scan = scan();
        let first = Rule {
            target: target.into(),
            ..Rule::default()
        };
        let preview = rules::classification_preview(
            &scan,
            &[first, fallback()],
            &scan.root.join("organized"),
        )
        .unwrap();
        assert_eq!(preview.items.len(), 1, "{target}");
        assert_eq!(preview.items[0].rule_name, "Fallback");
        assert_eq!(preview.warnings.len(), 1);
    }
}

#[test]
fn invalid_rule_template_still_refuses_instead_of_applying_a_different_rule() {
    let scan = scan();
    let invalid = Rule {
        target: "{unknown_token}".into(),
        ..Rule::default()
    };
    let preview =
        rules::classification_preview(&scan, &[invalid, fallback()], &scan.root.join("organized"))
            .unwrap();
    assert!(preview.items.is_empty());
    assert_eq!(preview.warnings.len(), 1);
    assert!(preview.warnings[0].contains("지원하지 않는 기호"));
}
