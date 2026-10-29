use folder_organizer_rust::{
    core::{self, ScanOptions},
    merging, recovery, reporting, rules,
};
use std::fs;
use std::io::{Cursor, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::AtomicBool;
use std::time::{SystemTime, UNIX_EPOCH};

struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        let id = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let p =
            std::env::temp_dir().join(format!("organizer-advanced-{}-{id}", std::process::id()));
        fs::create_dir(&p).unwrap();
        Self(p)
    }
    fn path(&self, name: &str) -> PathBuf {
        self.0.join(name)
    }
    fn scan(&self) -> core::ScanResult {
        core::scan(
            &self.0,
            &ScanOptions::default(),
            &AtomicBool::new(false),
            None,
        )
        .unwrap()
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}
fn archive(entries: &[(&str, &[u8])]) -> Vec<u8> {
    let mut z = zip::ZipWriter::new(Cursor::new(Vec::new()));
    for (name, bytes) in entries {
        z.start_file(
            *name,
            zip::write::SimpleFileOptions::default()
                .compression_method(zip::CompressionMethod::Deflated),
        )
        .unwrap();
        z.write_all(bytes).unwrap();
    }
    z.finish().unwrap().into_inner()
}

#[test]
fn ordered_rule_mapping_is_read_only_and_rejects_traversal() {
    let f = Fixture::new();
    fs::write(f.path("bill.txt"), b"bill").unwrap();
    let scan = f.scan();
    let rule = rules::Rule {
        name: "업무".into(),
        target: "{규칙}/{종류}/{수정일:%Y/%m}".into(),
        ..rules::Rule::default()
    };
    let rendered = rules::render_target(&rule, &scan.files[0]).unwrap();
    assert!(rendered.starts_with(Path::new("업무/문서")));
    let dest = f.path("sorted");
    let plan = rules::classification_preview(
        &scan,
        &[
            rule.clone(),
            rules::Rule {
                target: "never".into(),
                ..rule.clone()
            },
        ],
        &dest,
    )
    .unwrap();
    assert_eq!(plan.items.len(), 1);
    assert!(!dest.exists());
    assert!(plan.items[0].target.starts_with(dest));
    assert!(rules::render_target(
        &rules::Rule {
            target: "../{name}".into(),
            ..rule.clone()
        },
        &scan.files[0]
    )
    .is_err());
    assert!(rules::render_target(
        &rules::Rule {
            target: "{name.__class__}".into(),
            ..rule
        },
        &scan.files[0]
    )
    .is_err());
}

#[test]
fn missing_creation_date_never_falls_back_to_modified() {
    let f = Fixture::new();
    fs::write(f.path("a.txt"), b"x").unwrap();
    let mut scan = f.scan();
    scan.files[0].created_ns = None;
    let rule = rules::Rule {
        target: "{생성일:%Y-%m}".into(),
        ..rules::Rule::default()
    };
    let plan = rules::classification_preview(&scan, &[rule], &f.path("out")).unwrap();
    assert!(plan.items.is_empty());
    assert_eq!(plan.warnings.len(), 1);
}

#[test]
fn pre_epoch_fractional_dates_render_without_clamping_or_truncation() {
    use chrono::TimeZone;
    let f = Fixture::new();
    fs::write(f.path("historic.txt"), b"x").unwrap();
    let mut entry = f.scan().files.remove(0);
    let ns = -315_619_200_123_456_789i128;
    entry.modified_ns = ns;
    entry.created_ns = Some(ns);
    let rule = rules::Rule {
        target: "{수정일:%Y-%m-%d}/{생성일:%H-%M-%S}".into(),
        ..rules::Rule::default()
    };
    let actual = rules::render_target(&rule, &entry).unwrap();
    let date = chrono::Local
        .timestamp_opt(
            ns.div_euclid(1_000_000_000) as i64,
            ns.rem_euclid(1_000_000_000) as u32,
        )
        .single()
        .unwrap();
    let expected = PathBuf::from(date.format("%Y-%m-%d/%H-%M-%S").to_string());
    assert_eq!(actual, expected);
    assert!(!actual.to_string_lossy().starts_with("1970"));
}

#[test]
fn text_merge_preserves_code_fences_and_all_sources() {
    let f = Fixture::new();
    let a = f.path("a.md");
    let b = f.path("b.md");
    let original = b"same\n\n```\nline\n\nline\n```\n";
    fs::write(&a, original).unwrap();
    fs::write(&b, b"same\n\nnew\n").unwrap();
    let out = f.path("merged.md");
    let preview = merging::preview_merge(&[a.clone(), b.clone()], &out).unwrap();
    assert_eq!(preview.removed_units, 1);
    assert!(!out.exists());
    merging::execute_merge(&preview).unwrap();
    assert_eq!(fs::read(&a).unwrap(), original);
    let content = fs::read_to_string(out).unwrap();
    assert!(content.contains("```\nline\n\nline\n```"));
    assert!(content.contains("new"));
    assert!(merging::execute_merge(&preview).is_err());
}

#[test]
fn merge_refuses_changed_input_and_tampered_output() {
    let f = Fixture::new();
    let a = f.path("a.txt");
    let b = f.path("b.txt");
    fs::write(&a, b"old").unwrap();
    fs::write(&b, b"other").unwrap();
    let mut preview = merging::preview_merge(&[a.clone(), b], &f.path("merged.txt")).unwrap();
    preview.output = f.path("forged.txt");
    assert!(merging::execute_merge(&preview).is_err());
    assert!(!f.path("forged.txt").exists());
    preview.output = f.path("merged.txt");
    fs::write(&a, b"new content").unwrap();
    assert!(merging::execute_merge(&preview).is_err());
    assert!(!preview.output.exists());
}

#[test]
fn csv_merge_preserves_cell_boundaries_and_rejects_schema_conflicts() {
    let f = Fixture::new();
    let a = f.path("a.csv");
    let b = f.path("b.csv");
    fs::write(&a, b"name,value\n\"a,b\",\"line\rbreak\"\n").unwrap();
    fs::write(&b, b"name,value\n\"a,b\",\"line\rbreak\"\nnew,2\n").unwrap();
    let out = f.path("joined.csv");
    let preview = merging::preview_merge(&[a.clone(), b.clone()], &out).unwrap();
    assert_eq!(preview.removed_units, 1);
    merging::execute_merge(&preview).unwrap();
    let mut csv = csv::Reader::from_path(out).unwrap();
    let rows = csv.records().map(Result::unwrap).collect::<Vec<_>>();
    assert_eq!(&rows[0][0], "a,b");
    assert_eq!(&rows[0][1], "line\rbreak");
    assert_eq!(rows.len(), 2);
    fs::write(&b, b"name,value\n\"broken\"trailing,1\n").unwrap();
    assert!(merging::preview_merge(&[a.clone(), b.clone()], &f.path("bad-quotes.csv")).is_err());
    fs::write(b, b"different,columns\nx,1\n").unwrap();
    assert!(merging::preview_merge(&[a, f.path("b.csv")], &f.path("invalid.csv")).is_err());
}

#[test]
fn archive_health_and_repair_preserve_original_and_good_entries() {
    let f = Fixture::new();
    let source = f.path("sample.zip");
    let original = archive(&[("safe.txt", b"safe"), ("../escape.txt", b"bad")]);
    fs::write(&source, &original).unwrap();
    let health = recovery::check_health(&source).unwrap();
    assert!(!health.healthy);
    assert_eq!(health.recoverable_units, 1);
    let output = f.path("recovered.zip");
    let result = recovery::repair_to_new(&source, &output).unwrap();
    assert_eq!(result.recovered_units, 1);
    assert_eq!(fs::read(source).unwrap(), original);
    let z = zip::ZipArchive::new(Cursor::new(fs::read(&output).unwrap())).unwrap();
    assert_eq!(z.len(), 1);
    assert!(recovery::repair_to_new(&f.path("sample.zip"), &output).is_err());
}

#[test]
fn zip_local_header_salvage_requires_valid_crc() {
    let f = Fixture::new();
    let original = archive(&[("one.txt", b"recover me")]);
    let end = original
        .windows(4)
        .position(|s| s == b"PK\x01\x02")
        .unwrap();
    let source = f.path("broken.zip");
    fs::write(&source, &original[..end]).unwrap();
    let health = recovery::check_health(&source).unwrap();
    assert_eq!(health.recoverable_units, 1);
    let out = f.path("repaired.zip");
    assert_eq!(
        recovery::repair_to_new(&source, &out)
            .unwrap()
            .recovered_units,
        1
    );
}

#[test]
fn office_xml_health_rejects_external_entities() {
    let f = Fixture::new();
    let source = f.path("bad.docx");
    let body = archive(&[
        ("[Content_Types].xml", b"<Types/>"),
        ("_rels/.rels", b"<Relationships/>"),
        (
            "word/document.xml",
            b"<!DOCTYPE doc [<!ENTITY x SYSTEM 'file:///etc/passwd'>]><doc>&x;</doc>",
        ),
    ]);
    fs::write(&source, body).unwrap();
    let health = recovery::check_health(&source).unwrap();
    assert!(!health.healthy);
    assert!(health.issues.iter().any(|s| s.contains("엔터티")));
}

#[test]
fn image_health_and_new_png_recovery() {
    let f = Fixture::new();
    let source = f.path("tiny.png");
    image::DynamicImage::new_rgb8(2, 3).save(&source).unwrap();
    assert!(recovery::check_health(&source).unwrap().healthy);
    let original = fs::read(&source).unwrap();
    let out = f.path("recovered.png");
    assert_eq!(
        recovery::repair_to_new(&source, &out)
            .unwrap()
            .recovered_units,
        1
    );
    assert_eq!(fs::read(source).unwrap(), original);
    assert!(recovery::check_health(&out).unwrap().healthy);
}

#[test]
fn pdf_new_copy_preserves_source_and_rejects_unbounded_object_streams() {
    use lopdf::{dictionary, Object};
    let f = Fixture::new();
    let mut document = lopdf::Document::with_version("1.4");
    document.reference_table.cross_reference_type = lopdf::xref::XrefType::CrossReferenceTable;
    let pages = document.new_object_id();
    let page=document.add_object(dictionary!{"Type"=>"Page","Parent"=>pages,"MediaBox"=>vec![Object::Integer(0),Object::Integer(0),Object::Integer(100),Object::Integer(100)]});
    document.objects.insert(
        pages,
        dictionary! {"Type"=>"Pages","Kids"=>vec![Object::Reference(page)],"Count"=>1}.into(),
    );
    let catalog = document.add_object(dictionary! {"Type"=>"Catalog","Pages"=>pages});
    document.trailer.set("Root", catalog);
    let source = f.path("one.pdf");
    document.save(&source).unwrap();
    let original = fs::read(&source).unwrap();
    assert!(recovery::check_health(&source).unwrap().healthy);
    let output = f.path("recovered.pdf");
    assert_eq!(
        recovery::repair_to_new(&source, &output)
            .unwrap()
            .recovered_units,
        1
    );
    assert_eq!(fs::read(source).unwrap(), original);
    assert!(recovery::check_health(&output).unwrap().healthy);
    let guarded = f.path("compressed.pdf");
    fs::write(&guarded, b"%PDF-1.7 /Type /Obj#53tm").unwrap();
    let health = recovery::check_health(&guarded).unwrap();
    assert!(!health.healthy);
    assert!(health.issues.iter().any(|s| s.contains("메모리 제한")));
}

#[test]
fn exported_reports_escape_untrusted_names_and_refuse_overwrite() {
    let f = Fixture::new();
    fs::write(f.path("=SUM(1).txt"), b"x").unwrap();
    let scan = f.scan();
    let csv = f.path("report.csv");
    reporting::export_csv(&scan, &csv).unwrap();
    assert!(fs::read_to_string(&csv).unwrap().contains("'=SUM(1).txt"));
    assert!(reporting::export_csv(&scan, &csv).is_err());
    let html = f.path("report.html");
    reporting::export_html(
        &scan,
        &vec!["<script>alert(1)</script>"],
        &Vec::<String>::new(),
        &html,
    )
    .unwrap();
    let content = fs::read_to_string(html).unwrap();
    assert!(!content.contains("<script>"));
    assert!(content.contains("&lt;script&gt;"));
    let report = f.path("report.json");
    reporting::export_json(&scan, &Vec::<String>::new(), &Vec::<String>::new(), &report).unwrap();
    let json: serde_json::Value = serde_json::from_slice(&fs::read(report).unwrap()).unwrap();
    assert_eq!(json["engine"], "Rust");
}
