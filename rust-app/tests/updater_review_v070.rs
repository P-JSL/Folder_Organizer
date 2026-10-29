//! New legacy-package compatibility cases; old updater suites remain skipped.
use folder_organizer_rust::updater;
use serde_json::json;
use std::{fs, path::PathBuf};

struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        let mut random = [0; 16];
        getrandom::getrandom(&mut random).unwrap();
        let id: String = random.iter().map(|byte| format!("{byte:02x}")).collect();
        let root = std::env::temp_dir().join(format!("organizer-updater-review-v070-{id}"));
        fs::create_dir(&root).unwrap();
        fs::write(
            root.join("version.json"),
            serde_json::to_vec(&json!({
                "app_id":updater::APP_ID,"version":"0.7.0"
            }))
            .unwrap(),
        )
        .unwrap();
        for name in [
            updater::APP_EXECUTABLE,
            updater::CLI_EXECUTABLE,
            updater::HELPER_EXECUTABLE,
        ] {
            fs::write(root.join(name), b"MZ-test-fixture").unwrap();
        }
        Self(root)
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[test]
fn shipped_legacy_receipt_does_not_block_a_manually_upgraded_installation() {
    let fixture = Fixture::new();
    let receipt = include_bytes!("fixtures/rust_v060_build_info.json");
    fs::write(fixture.0.join("BUILD_INFO.json"), receipt).unwrap();
    updater::validate_install_contents(&fixture.0).unwrap();
    assert_eq!(
        fs::read(fixture.0.join("BUILD_INFO.json")).unwrap(),
        receipt
    );
}

#[test]
fn personal_data_disguised_as_a_build_receipt_still_blocks_replacement() {
    let fixture = Fixture::new();
    let mut receipt: serde_json::Value =
        serde_json::from_slice(include_bytes!("fixtures/rust_v060_build_info.json")).unwrap();
    receipt["personal_notes"] = json!("do not destroy this private document");
    let bytes = serde_json::to_vec(&receipt).unwrap();
    fs::write(fixture.0.join("BUILD_INFO.json"), &bytes).unwrap();
    assert!(updater::validate_install_contents(&fixture.0).is_err());
    assert_eq!(fs::read(fixture.0.join("BUILD_INFO.json")).unwrap(), bytes);
}

#[test]
fn altered_recorded_program_hash_or_unknown_receipt_cannot_be_auto_replaced() {
    for data in [
        json!({"repository":"P-JSL/Folder_Organizer","version":"0.6.0"}),
        {
            let mut receipt: serde_json::Value =
                serde_json::from_slice(include_bytes!("fixtures/rust_v060_build_info.json"))
                    .unwrap();
            receipt["program_sha256"][updater::APP_EXECUTABLE] = json!("a".repeat(64));
            receipt
        },
    ] {
        let fixture = Fixture::new();
        let bytes = serde_json::to_vec(&data).unwrap();
        fs::write(fixture.0.join("BUILD_INFO.json"), &bytes).unwrap();
        assert!(updater::validate_install_contents(&fixture.0).is_err());
        assert_eq!(fs::read(fixture.0.join("BUILD_INFO.json")).unwrap(), bytes);
    }
}

#[test]
fn exact_shipped_windows_crlf_receipt_is_allowed_without_modification() {
    let fixture = Fixture::new();
    let bytes = include_str!("fixtures/rust_v060_build_info.json").replace('\n', "\r\n");
    fs::write(fixture.0.join("BUILD_INFO.json"), bytes.as_bytes()).unwrap();
    updater::validate_install_contents(&fixture.0).unwrap();
    assert_eq!(
        fs::read(fixture.0.join("BUILD_INFO.json")).unwrap(),
        bytes.as_bytes()
    );
}

#[test]
fn duplicate_json_fields_cannot_hide_personal_data_in_a_receipt() {
    let fixture = Fixture::new();
    let bytes = include_str!("fixtures/rust_v060_build_info.json").replacen(
        "{",
        "{\n  \"repository\": \"private user notes that must survive\",",
        1,
    );
    // A generic JSON Value parser discards the earlier duplicate value; exact
    // known-document matching must preserve this unknown/personal document.
    let parsed: serde_json::Value = serde_json::from_str(&bytes).unwrap();
    assert_eq!(parsed["repository"], "P-JSL/Folder_Organizer");
    fs::write(fixture.0.join("BUILD_INFO.json"), bytes.as_bytes()).unwrap();
    assert!(updater::validate_install_contents(&fixture.0).is_err());
    assert_eq!(
        fs::read(fixture.0.join("BUILD_INFO.json")).unwrap(),
        bytes.as_bytes()
    );
}
