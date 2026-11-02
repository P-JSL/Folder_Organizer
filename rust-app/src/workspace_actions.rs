//! Action-capable single/multi-folder sessions. Serialized reports remain
//! read-only; every write is an authenticated plan for an actual source root.
use crate::{
    core::{AppResult, Progress},
    multiroot::WorkspaceSession,
    operations::{self, OperationPlan, OperationReceipt, TransferKind},
    rules::{self, Rule},
    similarity::{self, SimilarAnalysis, SimilarOptions},
};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};

/// Fields are private and no deserializer exists. Clones retain the same
/// one-shot session lease; callers can inspect but cannot replace root plans.
#[derive(Clone, Debug)]
pub struct BatchPlan {
    session: WorkspaceSession,
    plans: Vec<OperationPlan>,
    kind: TransferKind,
}

impl BatchPlan {
    pub fn plans(&self) -> &[OperationPlan] {
        &self.plans
    }
    pub fn item_count(&self) -> usize {
        self.plans.iter().map(|plan| plan.items.len()).sum()
    }
    pub fn kind(&self) -> TransferKind {
        self.kind
    }
}

#[derive(Clone, Debug)]
pub struct BatchReceipt {
    pub receipts: Vec<OperationReceipt>,
    pub completed: bool,
    pub cancelled: bool,
    pub error: Option<String>,
}

fn finish_plan(
    session: &WorkspaceSession,
    mut plans: Vec<OperationPlan>,
    kind: TransferKind,
) -> AppResult<BatchPlan> {
    session.require_active()?;
    let sources = session
        .files()
        .iter()
        .map(|entry| entry.path.clone())
        .collect::<Vec<_>>();
    operations::reserve_workspace_targets(&mut plans, &sources)?;
    operations::preflight_workspace(&plans)?;
    Ok(BatchPlan {
        session: session.clone(),
        plans,
        kind,
    })
}

/// The owner map contains each overlapping path exactly once. Source-specific
/// planning still checks original inventory snapshots and actual root bounds.
pub fn plan_transfer(
    session: &WorkspaceSession,
    paths: &[PathBuf],
    destination: Option<&Path>,
    kind: TransferKind,
) -> AppResult<BatchPlan> {
    session.require_active()?;
    if paths.is_empty() {
        return Err("정리할 파일을 체크하세요.".into());
    }
    let owners: HashMap<_, _> = session
        .scans()
        .iter()
        .enumerate()
        .flat_map(|(index, scan)| scan.files.iter().map(move |entry| (&entry.path, index)))
        .collect();
    let mut chosen = BTreeMap::<usize, Vec<PathBuf>>::new();
    let mut seen = HashSet::new();
    for path in paths {
        if !seen.insert(path) {
            return Err("한 파일이 여러 번 선택되었습니다.".into());
        }
        let owner = owners.get(path).ok_or("현재 분석 목록 밖의 파일입니다.")?;
        chosen.entry(*owner).or_default().push(path.clone());
    }
    let plans = chosen
        .into_iter()
        .map(|(index, paths)| {
            operations::plan_files(&session.scans()[index], &paths, destination, kind)
        })
        .collect::<AppResult<Vec<_>>>()?;
    finish_plan(session, plans, kind)
}

/// Rules operate on each real root's owned inventory, preserving that root's
/// relative paths. All proposed output names are reserved across the batch.
pub fn plan_classification(
    session: &WorkspaceSession,
    rules: &[Rule],
    destination: &Path,
    kind: TransferKind,
    protected: &[PathBuf],
) -> AppResult<BatchPlan> {
    session.require_active()?;
    if kind == TransferKind::Delete {
        return Err("규칙 분류는 복사 또는 이동을 선택하세요.".into());
    }
    let mut active_rules = rules.to_vec();
    for rule in &mut active_rules {
        rule.protected_folders.extend_from_slice(protected);
    }
    let mut plans = Vec::new();
    for scan in session.scans() {
        let preview = rules::classification_preview(scan, &active_rules, destination)?;
        if !preview.items.is_empty() {
            plans.push(operations::plan_mapped(scan, &preview.mappings(), kind)?);
        }
    }
    if plans.is_empty() {
        return Err("현재 규칙에 맞는 정리 대상이 없습니다.".into());
    }
    finish_plan(session, plans, kind)
}

/// A first-file action uses the original global group's authenticated proof.
/// It never repeats enumeration, hashing or byte comparison.
pub fn keep_first(
    session: &WorkspaceSession,
    protected: &[PathBuf],
    cancel: &AtomicBool,
    callback: Option<&dyn Fn(Progress)>,
) -> AppResult<BatchReceipt> {
    let result = (|| {
        session.require_active()?;
        let plans =
            operations::workspace_keep_first_plans(session.scans(), session.groups(), protected)?;
        let plan = finish_plan(session, plans, TransferKind::Delete)?;
        execute(&plan, cancel, callback)
    })();
    if result.is_err() {
        session.invalidate();
    }
    result
}

/// Claim the shared authority before the first check/write. Sorted source and
/// keeper root leases span preflight and every per-root journal execution.
/// Partial progress is recoverable, rather than an all-roots atomic promise.
pub fn execute(
    plan: &BatchPlan,
    cancel: &AtomicBool,
    callback: Option<&dyn Fn(Progress)>,
) -> AppResult<BatchReceipt> {
    plan.session.require_active()?;
    if plan
        .session
        .consumed
        .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
        .is_err()
    {
        return Err("이미 실행한 분석 결과입니다. 새로 분석하세요.".into());
    }
    let mut result = BatchReceipt {
        receipts: Vec::new(),
        completed: false,
        cancelled: false,
        error: None,
    };
    if cancel.load(Ordering::Relaxed) {
        result.cancelled = true;
        return Ok(result);
    }
    if plan.plans.is_empty() {
        result.completed = true;
        return Ok(result);
    }
    operations::preflight_workspace(&plan.plans)?;
    let roots = operations::workspace_lock_roots(&plan.plans, plan.session.scans())?;
    let locks = operations::WorkspaceLocks::acquire(&roots)?;
    operations::preflight_workspace(&plan.plans)?;
    for root_plan in &plan.plans {
        if cancel.load(Ordering::Relaxed) {
            result.cancelled = true;
            break;
        }
        match locks.execute(root_plan, cancel, callback) {
            Ok(receipt) => {
                let stopped = !receipt.completed || receipt.cancelled;
                if !receipt.errors.is_empty() {
                    result.error = Some(receipt.errors.join("\n"));
                }
                result.cancelled |= receipt.cancelled;
                result.receipts.push(receipt);
                if stopped {
                    break;
                }
            }
            Err(error) => {
                result.error = Some(error);
                break;
            }
        }
    }
    result.cancelled |= cancel.load(Ordering::Relaxed);
    result.completed = !result.cancelled
        && result.error.is_none()
        && result.receipts.len() == plan.plans.len()
        && result.receipts.iter().all(|receipt| receipt.completed);
    Ok(result)
}

pub fn analyze_similar(
    session: &WorkspaceSession,
    options: &SimilarOptions,
    cancel: &AtomicBool,
    callback: Option<&dyn Fn(Progress)>,
) -> AppResult<SimilarAnalysis> {
    session.require_active()?;
    similarity::find_similar_files_excluding(
        session.files(),
        true,
        false,
        options,
        session.groups(),
        cancel,
        callback,
    )
}
