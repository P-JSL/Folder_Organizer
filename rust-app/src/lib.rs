//! Native file inventory, byte-verified matching and recoverable operations.
pub mod cli;
pub mod core;
pub mod merging;
pub mod operations;
pub mod recovery;
pub mod reporting;
pub mod rules;
pub mod similarity;
pub mod updater;

#[cfg(feature = "gui")]
pub mod gui;

pub use core::{
    AppResult, DuplicateGroup, FileEntry, FileSnapshot, Progress, ScanOptions, ScanResult,
};
