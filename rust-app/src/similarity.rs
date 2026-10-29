//! Bounded name/text/image candidates. Similarity is never permission to delete.
use crate::core::{
    analyze_duplicates, safe_open, snapshot, snapshot_open_file, validate_group, AppResult,
    DuplicateGroup, FileEntry, FileSnapshot, Progress, ScanResult,
};
use image::{ImageDecoder, ImageReader};
use serde::{Deserialize, Serialize};
use std::collections::{BinaryHeap, HashMap, HashSet, VecDeque};
use std::io::{Cursor, Read};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};

const MAX_INDEX_KEYS: usize = 65_536;
const MAX_POSTINGS: usize = 32;
const MAX_KEYS_PER_FILE: usize = 64;
const MAX_CANDIDATES_PER_FILE: usize = 32;
const MAX_TEXT_BYTES: usize = 256 * 1024;
const MAX_WORDS: usize = 4096;
const TEXT_CACHE_ENTRIES: usize = 256;
const IMAGE_CACHE_ENTRIES: usize = 4096;
const MAX_IMAGE_BYTES: u64 = 64 * 1024 * 1024;
const MAX_IMAGE_PIXELS: u64 = 40_000_000;
const TEXT_SKETCH_KEYS: usize = 8;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct SimilarOptions {
    pub compare_names: bool,
    pub compare_text: bool,
    pub compare_images: bool,
    pub name_threshold: f32,
    pub text_threshold: f32,
    pub image_threshold: f32,
    pub max_pairs: usize,
}

impl Default for SimilarOptions {
    fn default() -> Self {
        Self {
            compare_names: true,
            compare_text: true,
            compare_images: true,
            name_threshold: 0.82,
            text_threshold: 0.90,
            image_threshold: 0.90,
            max_pairs: 25_000,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct SimilarPair {
    pub left: PathBuf,
    pub right: PathBuf,
    pub name_score: f32,
    pub text_score: Option<f32>,
    pub image_score: Option<f32>,
    pub reason: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct SimilarAnalysis {
    pub pairs: Vec<SimilarPair>,
    pub compared_pairs: usize,
    pub possible_pairs: u128,
    pub excluded_exact_pairs: usize,
    pub complete: bool,
    pub cancelled: bool,
    pub warnings: Vec<String>,
    /// Effective user choices, rather than an implicit engine default.
    pub options: SimilarOptions,
    pub candidate_index: CandidateIndexMetrics,
    /// Failed content loads or stale-pair checks; not a proof of dissimilarity.
    pub feature_failures: usize,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct CandidateIndexMetrics {
    pub enabled: bool,
    pub files_visited: usize,
    pub content_files_indexed: usize,
    pub candidate_lookups: usize,
    pub postings_scanned: usize,
    pub candidate_pairs_generated: usize,
    pub candidate_pairs_selected: usize,
    pub index_keys_retained_peak: usize,
    pub index_evictions: usize,
    pub index_key_limit: usize,
    pub posting_limit: usize,
    pub candidates_per_file: usize,
}

struct TextSignature {
    words: Vec<String>,
    snapshot: FileSnapshot,
}

struct FeatureCache<T> {
    values: HashMap<usize, Option<T>>,
    order: VecDeque<usize>,
    limit: usize,
}

impl<T> FeatureCache<T> {
    fn new(limit: usize) -> Self {
        Self {
            values: HashMap::new(),
            order: VecDeque::new(),
            limit,
        }
    }

    fn ensure(
        &mut self,
        id: usize,
        preserve: Option<usize>,
        load: impl FnOnce() -> AppResult<Option<T>>,
        report: &mut SimilarAnalysis,
    ) {
        if self.values.contains_key(&id) {
            return;
        }
        while self.values.len() >= self.limit {
            let Some(old) = self.order.pop_front() else {
                break;
            };
            if Some(old) == preserve {
                self.order.push_back(old);
            } else {
                self.values.remove(&old);
            }
        }
        let value = match load() {
            Ok(value) => value,
            Err(error) => {
                feature_warning(report, error);
                None
            }
        };
        self.values.insert(id, value);
        self.order.push_back(id);
    }
}

fn feature_warning(report: &mut SimilarAnalysis, error: String) {
    report.feature_failures += 1;
    report.complete = false;
    if report.warnings.len() < 100 {
        report.warnings.push(error);
    }
}

fn name_keys(entry: &FileEntry) -> Vec<String> {
    let stem = entry
        .path
        .file_stem()
        .unwrap_or_default()
        .to_string_lossy()
        .to_lowercase();
    let chars: Vec<char> = stem
        .chars()
        .filter(|ch| ch.is_alphanumeric())
        .take(256)
        .collect();
    if chars.len() < 2 {
        return vec![format!("n:{}", chars.into_iter().collect::<String>())];
    }
    let mut keys = HashSet::new();
    for pair in chars.windows(2) {
        keys.insert(format!("n:{}{}", pair[0], pair[1]));
        if keys.len() >= MAX_KEYS_PER_FILE {
            break;
        }
    }
    let mut keys: Vec<_> = keys.into_iter().collect();
    keys.sort();
    keys
}

fn dice(left: &[String], right: &[String]) -> f32 {
    if left.is_empty() || right.is_empty() {
        return 0.0;
    }
    let (mut a, mut b, mut equal) = (0, 0, 0);
    while a < left.len() && b < right.len() {
        match left[a].cmp(&right[b]) {
            std::cmp::Ordering::Less => a += 1,
            std::cmp::Ordering::Greater => b += 1,
            std::cmp::Ordering::Equal => {
                equal += 1;
                a += 1;
                b += 1;
            }
        }
    }
    2.0 * equal as f32 / (left.len() + right.len()) as f32
}

fn pair_rank(a: usize, b: usize) -> u64 {
    // Stable non-cryptographic sampling rank; this never establishes equality.
    let mut value = (a as u64).wrapping_mul(0x9e37_79b9_7f4a_7c15) ^ b as u64;
    value = (value ^ (value >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
    value = (value ^ (value >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
    value ^ (value >> 31)
}

fn text_words(entry: &FileEntry) -> AppResult<Option<TextSignature>> {
    if ![
        ".txt", ".md", ".csv", ".tsv", ".log", ".json", ".toml", ".yaml", ".yml", ".xml", ".ini",
        ".cfg",
    ]
    .contains(&entry.extension.as_str())
    {
        return Ok(None);
    }
    let mut file = safe_open(&entry.path)?;
    let before = snapshot_open_file(&file)?;
    if before.size != entry.size
        || before.modified_ns != entry.modified_ns
        || entry
            .identity
            .as_ref()
            .is_some_and(|identity| identity != &before)
    {
        return Err(format!(
            "목록 분석 후 파일이 바뀌었습니다: {}",
            entry.path.display()
        ));
    }
    let mut bytes = Vec::with_capacity(MAX_TEXT_BYTES.min(before.size as usize));
    (&mut file)
        .take(MAX_TEXT_BYTES as u64)
        .read_to_end(&mut bytes)
        .map_err(|error| error.to_string())?;
    if snapshot_open_file(&file)? != before || snapshot(&entry.path)? != before {
        return Err(format!(
            "텍스트를 읽는 동안 파일이 바뀌었습니다: {}",
            entry.path.display()
        ));
    }
    let value = match std::str::from_utf8(&bytes) {
        Ok(value) => value,
        Err(error) if error.error_len().is_none() && before.size > MAX_TEXT_BYTES as u64 => {
            std::str::from_utf8(&bytes[..error.valid_up_to()]).map_err(|error| error.to_string())?
        }
        Err(_) => return Ok(None),
    };
    let words: HashSet<String> = value
        .split(|ch: char| !ch.is_alphanumeric())
        .filter(|word| !word.is_empty())
        .take(MAX_WORDS)
        .map(str::to_lowercase)
        .collect();
    let mut words: Vec<_> = words.into_iter().collect();
    words.sort();
    Ok(Some(TextSignature {
        words,
        snapshot: before,
    }))
}

fn is_image(entry: &FileEntry) -> bool {
    [
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".ico",
    ]
    .contains(&entry.extension.as_str())
}

struct ImageSignature {
    hash: u64,
    pixels: Vec<u8>,
    aspect: f32,
    snapshot: FileSnapshot,
}

fn image_signature(entry: &FileEntry) -> AppResult<Option<ImageSignature>> {
    if !is_image(entry) {
        return Ok(None);
    }
    let mut file = safe_open(&entry.path)?;
    let before = snapshot_open_file(&file)?;
    if before.size > MAX_IMAGE_BYTES {
        return Err(format!(
            "64 MiB 초과 이미지 비교 건너뜀: {}",
            entry.path.display()
        ));
    }
    if before.size != entry.size
        || before.modified_ns != entry.modified_ns
        || entry
            .identity
            .as_ref()
            .is_some_and(|identity| identity != &before)
    {
        return Err(format!(
            "목록 분석 후 이미지가 바뀌었습니다: {}",
            entry.path.display()
        ));
    }
    let mut bytes = Vec::with_capacity(before.size as usize);
    (&mut file)
        .take(MAX_IMAGE_BYTES + 1)
        .read_to_end(&mut bytes)
        .map_err(|error| error.to_string())?;
    if bytes.len() as u64 != before.size
        || snapshot_open_file(&file)? != before
        || snapshot(&entry.path)? != before
    {
        return Err(format!(
            "읽는 동안 이미지가 바뀌었습니다: {}",
            entry.path.display()
        ));
    }
    let mut reader = ImageReader::new(Cursor::new(bytes))
        .with_guessed_format()
        .map_err(|error| error.to_string())?;
    let mut limits = image::Limits::default();
    limits.max_alloc = Some(192 * 1024 * 1024);
    reader.limits(limits);
    let mut decoder = reader
        .into_decoder()
        .map_err(|error| format!("이미지 읽기 실패 {}: {error}", entry.path.display()))?;
    let (width, height) = decoder.dimensions();
    if width == 0 || height == 0 || width as u64 * height as u64 > MAX_IMAGE_PIXELS {
        return Err(format!(
            "40 MP 초과 또는 크기 없는 이미지 건너뜀: {}",
            entry.path.display()
        ));
    }
    let orientation = decoder.orientation().map_err(|error| error.to_string())?;
    let mut image =
        image::DynamicImage::from_decoder(decoder).map_err(|error| error.to_string())?;
    image.apply_orientation(orientation);
    let aspect = image.width() as f32 / image.height() as f32;
    let tiny = image
        .resize_exact(9, 8, image::imageops::FilterType::Triangle)
        .to_rgba8();
    let luminance = |pixel: &image::Rgba<u8>| -> u32 {
        let alpha = pixel[3] as u32;
        let r = (pixel[0] as u32 * alpha + 255 * (255 - alpha)) / 255;
        let g = (pixel[1] as u32 * alpha + 255 * (255 - alpha)) / 255;
        let b = (pixel[2] as u32 * alpha + 255 * (255 - alpha)) / 255;
        299 * r + 587 * g + 114 * b
    };
    let mut hash = 0_u64;
    for y in 0..8 {
        for x in 0..8 {
            hash <<= 1;
            hash |= (luminance(tiny.get_pixel(x, y)) > luminance(tiny.get_pixel(x + 1, y))) as u64;
        }
    }
    let color = image
        .resize_exact(8, 8, image::imageops::FilterType::Triangle)
        .to_rgba8();
    let mut pixels = Vec::with_capacity(256);
    for pixel in color.pixels() {
        let alpha = pixel[3] as u32;
        for &channel in &pixel.0[..3] {
            pixels.push(((channel as u32 * alpha + 255 * (255 - alpha)) / 255) as u8);
        }
        pixels.push(pixel[3]);
    }
    if snapshot(&entry.path)? != before {
        return Err(format!(
            "이미지 해석 중 파일이 바뀌었습니다: {}",
            entry.path.display()
        ));
    }
    Ok(Some(ImageSignature {
        hash,
        pixels,
        aspect,
        snapshot: before,
    }))
}

fn image_score(left: &ImageSignature, right: &ImageSignature) -> f32 {
    let structure = 1.0 - (left.hash ^ right.hash).count_ones() as f32 / 64.0;
    let color = 1.0
        - left
            .pixels
            .iter()
            .zip(&right.pixels)
            .map(|(a, b)| a.abs_diff(*b) as u32)
            .sum::<u32>() as f32
            / (256.0 * 255.0);
    let aspect = (left.aspect / right.aspect).min(right.aspect / left.aspect);
    // Uniform but differently colored pictures cannot receive a high score
    // solely because their difference hashes are both constant.
    (structure * 0.55 + color * 0.35 + aspect * 0.10).clamp(0.0, 1.0)
}

fn word_rank(word: &str) -> u64 {
    // Stable retrieval sketch only: final scores still inspect actual features.
    word.as_bytes()
        .iter()
        .fold(0xcbf2_9ce4_8422_2325_u64, |hash, byte| {
            (hash ^ u64::from(*byte)).wrapping_mul(0x100_0000_01b3)
        })
}

fn text_keys(signature: &TextSignature) -> Vec<String> {
    let mut hashes: Vec<_> = signature.words.iter().map(|word| word_rank(word)).collect();
    hashes.sort_unstable();
    hashes.dedup();
    hashes.truncate(TEXT_SKETCH_KEYS);
    hashes
        .into_iter()
        .map(|hash| format!("t:{hash:016x}"))
        .collect()
}

fn image_keys(signature: &ImageSignature) -> Vec<String> {
    let mut keys: Vec<_> = (0..8)
        .map(|band| format!("ih:{band}:{}", (signature.hash >> (8 * band)) & 255))
        .collect();
    let mut means = [0_u32; 3];
    for pixel in signature.pixels.chunks_exact(4) {
        for (mean, value) in means.iter_mut().zip(pixel) {
            *mean += u32::from(*value);
        }
    }
    for mean in &mut means {
        *mean /= 64;
    }
    let aspect = (signature.aspect.ln() * 10.0).round() as i32;
    for shift in [0_u32, 16] {
        let color = means.map(|mean| (mean + shift) / 32);
        for bucket in [aspect - 1, aspect, aspect + 1] {
            keys.push(format!(
                "ic:{shift}:{bucket}:{}:{}:{}",
                color[0], color[1], color[2]
            ));
        }
    }
    keys
}

fn current_entry_snapshot(entry: &FileEntry) -> AppResult<FileSnapshot> {
    let state = snapshot(&entry.path)?;
    if state.size != entry.size
        || state.modified_ns != entry.modified_ns
        || entry
            .identity
            .as_ref()
            .is_some_and(|expected| expected != &state)
    {
        return Err(format!(
            "목록 분석 후 파일이 바뀌었습니다: {}",
            entry.path.display()
        ));
    }
    Ok(state)
}

fn indexed_pairs(
    scan: &ScanResult,
    options: &SimilarOptions,
    cancel: &AtomicBool,
    progress: Option<&dyn Fn(Progress)>,
    text_cache: &mut FeatureCache<TextSignature>,
    image_cache: &mut FeatureCache<ImageSignature>,
    report: &mut SimilarAnalysis,
) -> Vec<(usize, usize)> {
    let mut index: HashMap<String, VecDeque<usize>> = HashMap::new();
    let mut key_order = VecDeque::new();
    let mut sample = BinaryHeap::new();
    for (current, entry) in scan.files.iter().enumerate() {
        if cancel.load(Ordering::Relaxed) {
            report.cancelled = true;
            break;
        }
        let mut keys = Vec::new();
        if options.compare_text {
            text_cache.ensure(current, None, || text_words(entry), report);
            if let Some(Some(signature)) = text_cache.values.get(&current) {
                keys.extend(text_keys(signature));
            }
        }
        if options.compare_images && is_image(entry) {
            image_cache.ensure(current, None, || image_signature(entry), report);
            if let Some(Some(signature)) = image_cache.values.get(&current) {
                keys.extend(image_keys(signature));
            }
        }
        if !keys.is_empty() {
            report.candidate_index.content_files_indexed += 1;
        }
        // Enabled content always gets index slots. Disabled name comparison
        // cannot fill the candidate budget with unrelated name-only neighbors.
        if options.compare_names {
            keys.extend(name_keys(entry));
        }
        keys.truncate(MAX_KEYS_PER_FILE);
        let mut votes: HashMap<usize, (u32, u32)> = HashMap::new();
        for key in &keys {
            report.candidate_index.candidate_lookups += 1;
            if let Some(postings) = index.get(key) {
                for &previous in postings {
                    report.candidate_index.postings_scanned += 1;
                    let vote = votes.entry(previous).or_default();
                    if key.starts_with("n:") {
                        vote.1 += 1;
                    } else {
                        vote.0 += 1;
                    }
                }
            }
        }
        let mut candidates: Vec<_> = votes.into_iter().collect();
        candidates.sort_unstable_by_key(|(id, (content, name))| {
            (
                std::cmp::Reverse(*content),
                std::cmp::Reverse(*name),
                std::cmp::Reverse(*id),
            )
        });
        candidates.truncate(MAX_CANDIDATES_PER_FILE);
        for (previous, (content, name)) in candidates {
            report.candidate_index.candidate_pairs_generated += 1;
            // Strong shared content sketches precede weak name-only retrieval;
            // equal-quality candidates are sampled across the entire inventory.
            let quality = content * 16 + name.min(15);
            let candidate = (
                u32::MAX - quality,
                pair_rank(previous, current),
                previous,
                current,
            );
            if sample.len() < options.max_pairs {
                sample.push(candidate);
            } else if sample.peek().is_some_and(|top| candidate < *top) {
                sample.pop();
                sample.push(candidate);
            }
        }
        for key in keys {
            if !index.contains_key(&key) {
                if index.len() >= MAX_INDEX_KEYS {
                    if let Some(old) = key_order.pop_front() {
                        index.remove(&old);
                        report.candidate_index.index_evictions += 1;
                    }
                }
                key_order.push_back(key.clone());
            }
            let postings = index.entry(key).or_default();
            if postings.len() >= MAX_POSTINGS {
                postings.pop_front();
            }
            postings.push_back(current);
        }
        report.candidate_index.index_keys_retained_peak = report
            .candidate_index
            .index_keys_retained_peak
            .max(index.len());
        report.candidate_index.files_visited += 1;
        if current < 10 || (current + 1) % 32 == 0 || current + 1 == scan.files.len() {
            if let Some(callback) = progress {
                callback(Progress {
                    phase: "similar_index".into(),
                    completed: (current + 1) as u64,
                    total: Some(scan.files.len() as u64),
                    message: "이름·텍스트 특징·이미지 특징에서 제한된 비교 후보를 찾고 있습니다."
                        .into(),
                });
            }
        }
    }
    let mut pairs: Vec<_> = sample.into_iter().map(|(_, _, a, b)| (a, b)).collect();
    pairs.sort_unstable();
    report.candidate_index.candidate_pairs_selected = pairs.len();
    pairs
}

/// Visit the full inventory, sample bounded indexed candidates, and show its
/// coverage. Full pair enumeration is used only when it fits the pair budget.
pub fn find_similar(
    scan: &ScanResult,
    options: &SimilarOptions,
    cancel: &AtomicBool,
    progress: Option<&dyn Fn(Progress)>,
) -> AppResult<SimilarAnalysis> {
    let exact = analyze_duplicates(scan, cancel, progress)?;
    let mut report = find_similar_excluding(scan, options, &exact.groups, cancel, progress)?;
    report.complete &= exact.complete;
    for warning in exact.warnings {
        if report.warnings.len() >= 100 {
            break;
        }
        report.warnings.push(format!(
            "{} [{}]: {}",
            warning.path.display(),
            warning.phase,
            warning.message
        ));
    }
    Ok(report)
}

/// A GUI may pass its existing authenticated exact results to keep those pairs
/// out of the similar-candidate list without another content scan.
pub fn find_similar_excluding(
    scan: &ScanResult,
    options: &SimilarOptions,
    exact_groups: &[DuplicateGroup],
    cancel: &AtomicBool,
    progress: Option<&dyn Fn(Progress)>,
) -> AppResult<SimilarAnalysis> {
    if !options.compare_names && !options.compare_text && !options.compare_images {
        return Err("이름·텍스트·이미지 비교를 하나 이상 선택하세요.".into());
    }
    if !options.name_threshold.is_finite()
        || !options.text_threshold.is_finite()
        || !options.image_threshold.is_finite()
        || !(0.0..=1.0).contains(&options.name_threshold)
        || !(0.0..=1.0).contains(&options.text_threshold)
        || !(0.0..=1.0).contains(&options.image_threshold)
        || options.max_pairs == 0
        || options.max_pairs > 100_000
    {
        return Err("유사도 기준은 0~1, 비교 한도는 1~100,000이어야 합니다.".into());
    }
    let count = scan.files.len();
    let mut exact_membership = HashMap::new();
    for (index, group) in exact_groups.iter().enumerate() {
        validate_group(group)?;
        for path in &group.paths {
            exact_membership.insert(path.clone(), index);
        }
    }
    let possible = count as u128 * count.saturating_sub(1) as u128 / 2;
    let exhaustive = possible <= options.max_pairs as u128;
    let mut report = SimilarAnalysis {
        pairs: vec![],
        compared_pairs: 0,
        possible_pairs: possible,
        excluded_exact_pairs: 0,
        complete: exhaustive && !scan.cancelled && scan.warnings.is_empty(),
        cancelled: scan.cancelled,
        warnings: vec![],
        options: options.clone(),
        candidate_index: CandidateIndexMetrics {
            enabled: !exhaustive,
            index_key_limit: MAX_INDEX_KEYS,
            posting_limit: MAX_POSTINGS,
            candidates_per_file: MAX_CANDIDATES_PER_FILE,
            ..Default::default()
        },
        feature_failures: 0,
    };
    if !exhaustive {
        report.warnings.push("전체 목록의 활성화된 이름·텍스트 특징·이미지 특징에서 제한된 후보를 추린 결과입니다. 후보·인덱스·비교 한도로 일부 유사 파일을 놓칠 수 있습니다.".into());
    }
    if options.compare_text {
        report.warnings.push("텍스트는 UTF-8 앞 256 KiB·4,096단어의 어휘 집합을 비교합니다. 문장 의미나 전체 문서 동일성을 판정하지 않습니다.".into());
    }
    if options.compare_images {
        report.warnings.push("이미지는 40 MP·64 MiB 이하의 첫 프레임을 방향 보정 후 축소 구조·색·투명도·종횡비로 비교합니다. 전체 움직임·회전·크롭·의미를 판정하지 않습니다.".into());
        report.warnings.push("고주파 무늬·노이즈·균일한 이미지는 축소 특징이 겹쳐 서로 다른 파일도 높은 유사도를 받을 수 있습니다. 원본을 직접 확인하세요.".into());
    }
    report.warnings.push(
        "유사도 100%도 완전 중복의 증거가 아닙니다. 유사 결과로 자동 삭제하지 않습니다.".into(),
    );
    let mut text_cache = FeatureCache::new(TEXT_CACHE_ENTRIES);
    let mut image_cache = FeatureCache::new(IMAGE_CACHE_ENTRIES);
    let mut pairs = Vec::new();
    if !report.cancelled {
        if exhaustive {
            'outer: for left in 0..count {
                for right in left + 1..count {
                    if cancel.load(Ordering::Relaxed) {
                        report.cancelled = true;
                        report.complete = false;
                        break 'outer;
                    }
                    pairs.push((left, right));
                }
            }
        } else {
            pairs = indexed_pairs(
                scan,
                options,
                cancel,
                progress,
                &mut text_cache,
                &mut image_cache,
                &mut report,
            );
        }
    }
    let pair_count = pairs.len();
    for (left, right) in pairs {
        if cancel.load(Ordering::Relaxed) {
            report.cancelled = true;
            report.complete = false;
            break;
        }
        let a = &scan.files[left];
        let b = &scan.files[right];
        if exact_membership
            .get(&a.path)
            .is_some_and(|group| exact_membership.get(&b.path) == Some(group))
        {
            report.excluded_exact_pairs += 1;
            continue;
        }
        let left_state = match current_entry_snapshot(a) {
            Ok(state) => state,
            Err(error) => {
                feature_warning(&mut report, error);
                continue;
            }
        };
        let right_state = match current_entry_snapshot(b) {
            Ok(state) => state,
            Err(error) => {
                feature_warning(&mut report, error);
                continue;
            }
        };
        let name_score = if options.compare_names {
            dice(&name_keys(a), &name_keys(b))
        } else {
            0.0
        };
        let mut text_score = None;
        let mut image_similarity = None;
        if options.compare_text {
            text_cache.ensure(left, Some(right), || text_words(a), &mut report);
            text_cache.ensure(right, Some(left), || text_words(b), &mut report);
            if let (Some(Some(left_feature)), Some(Some(right_feature))) =
                (text_cache.values.get(&left), text_cache.values.get(&right))
            {
                if left_feature.snapshot != left_state || right_feature.snapshot != right_state {
                    feature_warning(
                        &mut report,
                        "후보를 찾은 뒤 텍스트가 변경되어 해당 쌍을 건너뜁니다.".into(),
                    );
                    continue;
                }
                text_score = Some(dice(&left_feature.words, &right_feature.words));
            }
        }
        if options.compare_images && is_image(a) && is_image(b) {
            image_cache.ensure(left, Some(right), || image_signature(a), &mut report);
            image_cache.ensure(right, Some(left), || image_signature(b), &mut report);
            if let (Some(Some(left_feature)), Some(Some(right_feature))) = (
                image_cache.values.get(&left),
                image_cache.values.get(&right),
            ) {
                if left_feature.snapshot != left_state || right_feature.snapshot != right_state {
                    feature_warning(
                        &mut report,
                        "후보를 찾은 뒤 이미지가 변경되어 해당 쌍을 건너뜁니다.".into(),
                    );
                    continue;
                }
                image_similarity = Some(image_score(left_feature, right_feature));
            }
        }
        if snapshot(&a.path).as_ref() != Ok(&left_state)
            || snapshot(&b.path).as_ref() != Ok(&right_state)
        {
            feature_warning(
                &mut report,
                "유사도를 계산하는 동안 파일이 변경되어 해당 쌍을 건너뜁니다.".into(),
            );
            continue;
        }
        report.compared_pairs += 1;
        if (options.compare_names && name_score >= options.name_threshold)
            || text_score.is_some_and(|score| score >= options.text_threshold)
        {
            // Image evidence is reported independently; no weak signal hides
            // another enabled criterion.
            report.pairs.push(SimilarPair {
                left: a.path.clone(),
                right: b.path.clone(),
                name_score,
                text_score,
                image_score: image_similarity,
                reason: "이름/제한된 텍스트/이미지 유사 후보 · 삭제 근거 아님".into(),
            });
        } else if image_similarity.is_some_and(|score| score >= options.image_threshold) {
            report.pairs.push(SimilarPair {
                left: a.path.clone(),
                right: b.path.clone(),
                name_score,
                text_score,
                image_score: image_similarity,
                reason: "이미지 유사 후보 · 삭제 근거 아님".into(),
            });
        }
        if report.compared_pairs % 32 == 0 || report.compared_pairs == pair_count {
            if let Some(callback) = progress {
                callback(Progress {
                    phase: "similarity".into(),
                    completed: report.compared_pairs as u64,
                    total: Some(pair_count as u64),
                    message: "유사 후보를 비교하고 있습니다.".into(),
                });
            }
        }
    }
    Ok(report)
}
