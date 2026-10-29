//! New Rust update contracts; none of these repeat Python's prior test suite.
#![cfg(not(windows))]
use folder_organizer_rust::updater::{self, UpdatePlan};
use serde_json::json;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::AtomicBool;
use std::sync::{Arc, Barrier, Mutex};

static ENVIRONMENT: Mutex<()> = Mutex::new(());

struct Fixture {
    root: PathBuf,
    old_cache: Option<std::ffi::OsString>,
}
impl Fixture {
    fn new() -> Self {
        let mut random = [0_u8; 12];
        getrandom::getrandom(&mut random).unwrap();
        let id: String = random.iter().map(|n| format!("{n:02x}")).collect();
        let root = std::env::temp_dir().join(format!("rust-updater-v1-{id}"));
        fs::create_dir(&root).unwrap();
        let old_cache = std::env::var_os("XDG_CACHE_HOME");
        std::env::set_var("XDG_CACHE_HOME", root.join("cache"));
        Self { root, old_cache }
    }
    fn install(&self) -> PathBuf {
        let install = self.root.join("Programs").join(updater::APP_DIRECTORY);
        package(&install, "0.5.0");
        install
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        if let Some(old) = self.old_cache.take() {
            std::env::set_var("XDG_CACHE_HOME", old);
        } else {
            std::env::remove_var("XDG_CACHE_HOME");
        }
        let _ = fs::remove_dir_all(&self.root);
    }
}

fn package(path: &Path, version: &str) {
    fs::create_dir_all(path).unwrap();
    fs::write(
        path.join("version.json"),
        serde_json::to_vec(&json!({"app_id":updater::APP_ID,"version":version})).unwrap(),
    )
    .unwrap();
    for name in [
        updater::APP_EXECUTABLE,
        updater::HELPER_EXECUTABLE,
        updater::CLI_EXECUTABLE,
    ] {
        fs::write(path.join(name), format!("MZ-{version}-{name}")).unwrap();
    }
}

fn plan(install: &Path, version: &str, id: &str) -> UpdatePlan {
    let work = updater::storage_root().unwrap().join(id);
    let stage = work.join("payload").join(updater::APP_DIRECTORY);
    package(&stage, version);
    let plan = UpdatePlan {
        schema: 1,
        app_id: updater::APP_ID.into(),
        id: id.into(),
        version: version.into(),
        original_version: "0.5.0".into(),
        install_dir: install.into(),
        stage_dir: stage.clone(),
        plan_path: work.join("plan.json"),
        parent_pid: std::process::id(),
        parent_created: 0,
        files: updater::manifest(&stage, &AtomicBool::new(false)).unwrap(),
        archive_sha256: "a".repeat(64),
    };
    fs::write(&plan.plan_path, serde_json::to_vec(&plan).unwrap()).unwrap();
    plan
}

fn archive(path: &Path, names: &[&str]) {
    let mut zip = zip::ZipWriter::new(fs::File::create(path).unwrap());
    for name in names {
        zip.start_file(*name, zip::write::SimpleFileOptions::default())
            .unwrap();
        zip.write_all(b"payload").unwrap();
    }
    zip.finish().unwrap();
}

#[test]
fn separate_rust_checksum_and_stable_release_contract() {
    let own = "a".repeat(64);
    let python = "b".repeat(64);
    let text = format!(
        "\u{feff}{python}  FolderOrganizer-Windows-x64.zip\n{own} *{}\n",
        updater::WINDOWS_ASSET
    );
    assert_eq!(updater::expected_checksum(text.as_bytes()).unwrap(), own);
    assert!(updater::expected_checksum(
        format!(
            "{own}  {}\n{own}  {}\n",
            updater::WINDOWS_ASSET,
            updater::WINDOWS_ASSET
        )
        .as_bytes()
    )
    .is_err());
    let release = json!({"tag_name":"v0.6.0","draft":false,"prerelease":false,"assets":[
        {"name":"FolderOrganizer-Windows-x64.zip","size":100,"browser_download_url":"https://github.com/P-JSL/Folder_Organizer/releases/download/v0.6.0/FolderOrganizer-Windows-x64.zip"},
        {"name":"SHA256SUMS.txt","size":98,"browser_download_url":"https://github.com/P-JSL/Folder_Organizer/releases/download/v0.6.0/SHA256SUMS.txt"}]});
    assert!(updater::parse_release_metadata(&release, "0.5.0")
        .unwrap()
        .is_none());
    let mut release = release;
    release["assets"][0] = json!({"name":updater::WINDOWS_ASSET,"size":100,"browser_download_url":format!("https://github.com/P-JSL/Folder_Organizer/releases/download/v0.6.0/{}",updater::WINDOWS_ASSET)});
    assert_eq!(
        updater::parse_release_metadata(&release, "0.5.0")
            .unwrap()
            .unwrap()
            .version,
        "0.6.0"
    );
    release["assets"][0]["browser_download_url"] = json!("https://evil.example/update.zip");
    assert!(updater::parse_release_metadata(&release, "0.5.0").is_err());
}

#[test]
fn urls_versions_and_windows_paths_fail_closed() {
    for bad in [
        "http://github.com/x",
        "https://github.com.evil/x",
        "https://github.com@evil/x",
        "https://github.com:443/x",
        "https://api.github.com/repos/other/repo/releases/latest",
    ] {
        assert!(updater::safe_url(bad).is_err(), "{bad}");
    }
    assert!(
        updater::safe_url("https://release-assets.githubusercontent.com/files/1?token=opaque")
            .is_ok()
    );
    for bad in ["1.0", "01.0.0", "1.0.0-beta", "1.0.0+build", "1.0.-1"] {
        assert!(updater::version_tuple(bad).is_err());
    }
    for bad in [
        "../escape",
        "a/../b",
        "a\\b",
        "a//b",
        "CON.txt",
        "a/COM1.log",
        "a/trailing.",
        "a/name:stream",
        "/absolute",
    ] {
        assert!(updater::safe_relative(bad).is_err(), "{bad}");
    }
}

#[test]
fn archive_rejects_traversal_case_aliases_and_cancel_before_writing() {
    let _serial = ENVIRONMENT.lock().unwrap();
    let fixture = Fixture::new();
    let zip = fixture.root.join("bad.zip");
    archive(&zip, &["FolderOrganizerRust/../../outside.txt"]);
    assert!(
        updater::extract_archive(&zip, &fixture.root.join("payload"), &AtomicBool::new(false))
            .is_err()
    );
    assert!(!fixture.root.join("outside.txt").exists());
    fs::remove_file(&zip).unwrap();
    archive(
        &zip,
        &["FolderOrganizerRust/A.txt", "FolderOrganizerRust/a.txt"],
    );
    assert!(
        updater::extract_archive(&zip, &fixture.root.join("case"), &AtomicBool::new(false))
            .is_err()
    );
    assert!(updater::extract_archive(
        &zip,
        &fixture.root.join("cancelled"),
        &AtomicBool::new(true)
    )
    .is_err());
    assert!(!fixture.root.join("cancelled").exists());
}

#[test]
fn verified_directory_swap_retains_original_and_records_result() {
    let _serial = ENVIRONMENT.lock().unwrap();
    let fixture = Fixture::new();
    let install = fixture.install();
    let plan = plan(&install, "0.6.0", &"1".repeat(32));
    let loaded = updater::load_plan(&plan.plan_path).unwrap();
    assert_eq!(loaded, plan);
    let result = updater::apply_update(&plan, false, false).unwrap();
    assert_eq!(updater::read_marker(&install).unwrap(), "0.6.0");
    let backup = PathBuf::from(result["backup"].as_str().unwrap());
    assert_eq!(updater::read_marker(&backup).unwrap(), "0.5.0");
    assert_eq!(updater::last_update_result().unwrap()["status"], "updated");
    assert!(updater::apply_update(&plan, false, false).is_err());
}

#[test]
fn changed_stage_or_personal_file_never_replaces_installation() {
    let _serial = ENVIRONMENT.lock().unwrap();
    let fixture = Fixture::new();
    let install = fixture.install();
    let staged = plan(&install, "0.6.0", &"2".repeat(32));
    fs::write(
        staged.stage_dir.join(updater::APP_EXECUTABLE),
        b"changed after verification",
    )
    .unwrap();
    assert!(updater::apply_update(&staged, false, false).is_err());
    assert_eq!(updater::read_marker(&install).unwrap(), "0.5.0");
    let personal = plan(&install, "0.7.0", &"3".repeat(32));
    fs::write(install.join("my-document.txt"), b"must stay").unwrap();
    assert!(updater::apply_update(&personal, false, false).is_err());
    assert_eq!(
        fs::read(install.join("my-document.txt")).unwrap(),
        b"must stay"
    );
}

#[test]
fn one_installation_lock_covers_distinct_plans_and_cannot_downgrade() {
    let _serial = ENVIRONMENT.lock().unwrap();
    let fixture = Fixture::new();
    let install = fixture.install();
    let lower = plan(&install, "0.6.0", &"4".repeat(32));
    let higher = plan(&install, "0.7.0", &"5".repeat(32));
    let lock = updater::InstallUpdateLock::acquire(&install).unwrap();
    assert!(updater::InstallUpdateLock::acquire(&install).is_err());
    assert!(updater::apply_update(&lower, false, false).is_err());
    drop(lock);
    let barrier = Arc::new(Barrier::new(2));
    let first = barrier.clone();
    let low = std::thread::spawn(move || {
        first.wait();
        updater::apply_update(&lower, false, false)
    });
    let high = std::thread::spawn(move || {
        barrier.wait();
        updater::apply_update(&higher, false, false)
    });
    let outcomes = [low.join().unwrap(), high.join().unwrap()];
    assert_eq!(outcomes.iter().filter(|value| value.is_ok()).count(), 1);
    let winner = outcomes
        .iter()
        .find_map(|result| result.as_ref().ok())
        .unwrap();
    assert_eq!(
        updater::read_marker(&install).unwrap(),
        winner["version"].as_str().unwrap()
    );
    assert_eq!(
        updater::read_marker(Path::new(winner["backup"].as_str().unwrap())).unwrap(),
        "0.5.0"
    );
}

#[test]
fn symlinked_storage_or_package_member_is_refused() {
    use std::os::unix::fs::symlink;
    let _serial = ENVIRONMENT.lock().unwrap();
    let fixture = Fixture::new();
    let install = fixture.install();
    let outside = fixture.root.join("outside");
    fs::create_dir(&outside).unwrap();
    symlink(&outside, install.join("licenses")).unwrap();
    assert!(updater::validate_install_contents(&install).is_err());
    assert!(updater::manifest(&install, &AtomicBool::new(false)).is_err());
}
