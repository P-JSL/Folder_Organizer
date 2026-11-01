//! New additive CLI/export boundaries; no old suites are repeated.
use folder_organizer_rust::cli;
use std::{fs, path::PathBuf};

struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        let mut random = [0_u8; 10];
        getrandom::getrandom(&mut random).unwrap();
        let name: String = random.iter().map(|v| format!("{v:02x}")).collect();
        let path = std::env::temp_dir().join(format!("organizer-cli-multi-{name}"));
        fs::create_dir(&path).unwrap();
        Self(path)
    }
}
impl Drop for Fixture { fn drop(&mut self) { let _ = fs::remove_dir_all(&self.0); } }
fn args(values: &[&str]) -> Vec<String> { values.iter().map(|v| (*v).to_owned()).collect() }

#[test]
fn new_cli_exports_exact_cross_folder_groups_without_operation_authority() {
    let fixture = Fixture::new();
    let left = fixture.0.join("left"); let right = fixture.0.join("right");
    fs::create_dir(&left).unwrap(); fs::create_dir(&right).unwrap();
    let raw = b"actual cross-folder byte proof";
    fs::write(left.join("original.txt"), raw).unwrap();
    fs::write(right.join("copy.txt"), raw).unwrap();
    let path = fixture.0.join("multi.json");
    let arguments = args(&[left.to_str().unwrap(), "--add-folder", right.to_str().unwrap(),
        "--duplicates", "--json-multiroot", path.to_str().unwrap()]);
    assert_eq!(cli::run(arguments.clone()).unwrap(), 0);
    let bytes = fs::read(&path).unwrap();
    let value: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(value["schema"], "folder-organizer.multiroot.v1");
    assert_eq!(value["action_authority"], false);
    assert_eq!(value["files"].as_array().unwrap().len(), 2);
    assert_eq!(value["duplicates"]["groups"].as_array().unwrap().len(), 1);
    assert_eq!(value["duplicates"]["groups"][0]["group_source_count"], 2);
    assert_eq!(value["duplicates"]["groups"][0]["action_authority"], false);
    assert!(value["files"][0]["modified_ns"].is_string());
    assert!(cli::run(arguments).is_err());
    assert_eq!(fs::read(&path).unwrap(), bytes);
    assert_eq!(fs::read(left.join("original.txt")).unwrap(), raw);
    assert_eq!(fs::read(right.join("copy.txt")).unwrap(), raw);
}

#[test]
fn new_cli_missing_root_propagates_partial_without_hiding_healthy_groups() {
    let fixture = Fixture::new(); let root = fixture.0.join("healthy"); fs::create_dir(&root).unwrap();
    for name in ["a.bin", "b.bin"] { fs::write(root.join(name), b"verified healthy data").unwrap(); }
    let path = fixture.0.join("partial.json"); let absent = fixture.0.join("offline");
    assert_eq!(cli::run(args(&[root.to_str().unwrap(), "--add-folder", absent.to_str().unwrap(),
        "--duplicates", "--json-multiroot", path.to_str().unwrap()])).unwrap(), 2);
    let value: serde_json::Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    assert_eq!(value["complete"], false);
    assert_eq!(value["duplicates"]["complete"], false);
    assert_eq!(value["duplicates"]["groups"].as_array().unwrap().len(), 1);
}

#[test]
fn new_cli_rejects_mixed_contracts_and_invalid_workers_before_filesystem_reads() {
    let error = cli::run(args(&["/missing", "--add-folder", "/also-missing", "--similar"])).unwrap_err();
    assert!(error.contains("다중 폴더"));
    for value in ["0", "5", "garbage"] {
        let error = cli::run(args(&["/missing", "--workers", value])).unwrap_err();
        assert!(error.contains("--workers"));
    }
}

#[test]
fn new_multiroot_export_cannot_write_inside_a_selected_original_root() {
    let fixture = Fixture::new(); let path = fixture.0.join("forbidden.json");
    let error = cli::run(args(&[fixture.0.to_str().unwrap(), "--json-multiroot", path.to_str().unwrap()])).unwrap_err();
    assert!(error.contains("원본 폴더 밖"));
    assert!(!path.exists());
}
