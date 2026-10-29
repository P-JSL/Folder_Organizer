//! Windows FFI/update cases only; Linux-passed cases are not repeated here.
#![cfg(windows)]
use folder_organizer_rust::updater;
use std::fs;
use std::path::PathBuf;
use std::process::Command;
use std::sync::atomic::AtomicBool;
use std::sync::Mutex;
use std::time::Instant;

static ENVIRONMENT: Mutex<()> = Mutex::new(());

unsafe fn creation_time(pid: u32) -> u64 {
    use windows_sys::Win32::Foundation::{CloseHandle, FILETIME};
    use windows_sys::Win32::System::Threading::{
        GetProcessTimes, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
    };
    let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
    assert!(!handle.is_null());
    let mut created: FILETIME = std::mem::zeroed();
    let mut exited: FILETIME = std::mem::zeroed();
    let mut kernel: FILETIME = std::mem::zeroed();
    let mut user: FILETIME = std::mem::zeroed();
    assert_ne!(
        GetProcessTimes(handle, &mut created, &mut exited, &mut kernel, &mut user),
        0
    );
    CloseHandle(handle);
    (u64::from(created.dwHighDateTime) << 32) | u64::from(created.dwLowDateTime)
}

#[test]
fn process_handle_wait_rejects_reused_identity_and_waits_for_actual_exit() {
    let pid = std::process::id();
    let created = unsafe { creation_time(pid) };
    assert!(updater::wait_for_process(pid, created + 1).is_err());
    let mut child = Command::new("powershell.exe")
        .args([
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Start-Sleep -Milliseconds 600",
        ])
        .spawn()
        .unwrap();
    let token = unsafe { creation_time(child.id()) };
    let started = Instant::now();
    updater::wait_for_process(child.id(), token).unwrap();
    assert!(started.elapsed().as_millis() >= 200);
    assert!(child.wait().unwrap().success());
}

#[test]
fn canonical_case_aliases_share_one_windows_lock_without_replacing_files() {
    let _serial = ENVIRONMENT.lock().unwrap();
    let mut random = [0_u8; 12];
    getrandom::getrandom(&mut random).unwrap();
    let id: String = random.iter().map(|n| format!("{n:02x}")).collect();
    let root = std::env::temp_dir().join(format!("rust-updater-windows-{id}"));
    let install = root.join("Apps").join("FolderOrganizerRust");
    fs::create_dir_all(&install).unwrap();
    let lock = updater::InstallUpdateLock::acquire(&install).unwrap();
    let alias = PathBuf::from(install.to_string_lossy().to_uppercase());
    assert!(updater::InstallUpdateLock::acquire(&alias).is_err());
    assert!(updater::InstallUpdateLock::acquire(&fs::canonicalize(&install).unwrap()).is_err());
    drop(lock);
    assert!(updater::InstallUpdateLock::acquire(&alias).is_ok());
    let locks: Vec<_> = fs::read_dir(install.parent().unwrap())
        .unwrap()
        .filter_map(Result::ok)
        .filter(|entry| entry.file_name().to_string_lossy().ends_with(".lock"))
        .collect();
    assert_eq!(locks.len(), 1);
    assert_eq!(locks[0].metadata().unwrap().len(), 0);
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn native_movefileex_swap_refuses_existing_backup_and_keeps_previous_version() {
    let _serial = ENVIRONMENT.lock().unwrap();
    let mut random = [0_u8; 12];
    getrandom::getrandom(&mut random).unwrap();
    let id: String = random.iter().map(|n| format!("{n:02x}")).collect();
    let root = std::env::temp_dir().join(format!("rust-updater-native-swap-{id}"));
    fs::create_dir(&root).unwrap();
    struct Restore(Option<std::ffi::OsString>);
    impl Drop for Restore {
        fn drop(&mut self) {
            if let Some(value) = &self.0 {
                std::env::set_var("LOCALAPPDATA", value)
            } else {
                std::env::remove_var("LOCALAPPDATA")
            }
        }
    }
    let _restore = Restore(std::env::var_os("LOCALAPPDATA"));
    std::env::set_var("LOCALAPPDATA", root.join("AppData"));
    let install = root.join("Programs").join(updater::APP_DIRECTORY);
    let identifier = "8".repeat(32);
    let work = updater::storage_root().unwrap().join(&identifier);
    let stage = work.join("payload").join(updater::APP_DIRECTORY);
    for (path, version) in [(&install, "0.5.0"), (&stage, "0.6.0")] {
        fs::create_dir_all(path).unwrap();
        fs::write(
            path.join("version.json"),
            serde_json::to_vec(&serde_json::json!({"app_id":updater::APP_ID,"version":version}))
                .unwrap(),
        )
        .unwrap();
        for name in [
            updater::APP_EXECUTABLE,
            updater::HELPER_EXECUTABLE,
            updater::CLI_EXECUTABLE,
        ] {
            fs::write(path.join(name), format!("MZ-native-{version}-{name}")).unwrap();
        }
    }
    let plan = updater::UpdatePlan {
        schema: 1,
        app_id: updater::APP_ID.into(),
        id: identifier.clone(),
        version: "0.6.0".into(),
        original_version: "0.5.0".into(),
        install_dir: install.clone(),
        stage_dir: stage.clone(),
        plan_path: work.join("plan.json"),
        parent_pid: std::process::id(),
        parent_created: unsafe { creation_time(std::process::id()) },
        files: updater::manifest(&stage, &AtomicBool::new(false)).unwrap(),
        archive_sha256: "a".repeat(64),
    };
    fs::write(&plan.plan_path, serde_json::to_vec(&plan).unwrap()).unwrap();
    let backup =
        install.with_file_name(format!("{}.previous-{identifier}", updater::APP_DIRECTORY));
    fs::create_dir(&backup).unwrap();
    fs::write(backup.join("existing.txt"), b"preserve").unwrap();
    assert!(updater::apply_update(&plan, false, false).is_err());
    assert_eq!(fs::read(backup.join("existing.txt")).unwrap(), b"preserve");
    assert_eq!(updater::read_marker(&install).unwrap(), "0.5.0");
    fs::remove_dir_all(&backup).unwrap();
    updater::apply_update(&plan, false, false).unwrap();
    assert_eq!(updater::read_marker(&install).unwrap(), "0.6.0");
    assert_eq!(updater::read_marker(&backup).unwrap(), "0.5.0");
    fs::remove_dir_all(root).unwrap();
}
