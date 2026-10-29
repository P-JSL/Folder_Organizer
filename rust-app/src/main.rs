#![cfg_attr(all(windows, not(debug_assertions)), windows_subsystem = "windows")]

fn main() -> eframe::Result<()> {
    if std::env::args().any(|argument| argument == "--cli") {
        std::process::exit(folder_organizer_rust::cli::main());
    }
    folder_organizer_rust::gui::run()
}
