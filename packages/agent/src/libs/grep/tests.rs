use std::fs;
use std::os::unix::fs::symlink;
use std::time::Duration;

use tempfile::TempDir;

use super::{search, Hit, Query};
use crate::libs::confine::Root;

const CONFIG: &str = "instances:\n  web-01.example.com:\n    flavor: small\n";

fn tree() -> (TempDir, Root) {
    let dir = TempDir::new().unwrap();
    let base = fs::canonicalize(dir.path()).unwrap();
    let root = base.join("alpha");
    fs::create_dir_all(root.join("infra")).unwrap();
    fs::create_dir_all(root.join("build")).unwrap();
    fs::write(root.join("infra/web.yaml"), CONFIG).unwrap();
    fs::write(root.join("build/web.yaml"), CONFIG).unwrap();
    fs::write(root.join(".hidden.yaml"), CONFIG).unwrap();
    fs::write(root.join(".env"), "HOST=web-01.example.com\n").unwrap();
    fs::write(base.join("outside.txt"), "web-01.example.com\n").unwrap();
    (dir, Root::new(root))
}

fn query(pattern: &str) -> Query {
    Query {
        pattern: pattern.into(),
        ignore_lines: Vec::new(),
        path_glob: None,
        limit: 50,
        fixed: false,
        within: Duration::from_secs(60),
    }
}

fn hits(root: &Root, query: &Query) -> Vec<Hit> {
    let mut found = Vec::new();
    search(root, query, &mut |hit| {
        found.push(hit);
        true
    })
    .unwrap();
    found
}

fn paths(root: &Root, query: &Query) -> Vec<String> {
    hits(root, query).into_iter().map(|hit| hit.path).collect()
}

#[test]
fn a_match_names_its_file_and_line() {
    let (_dir, root) = tree();
    let mut asked = query("web-01");
    asked.ignore_lines = vec!["build/".into(), ".hidden.yaml".into()];
    assert_eq!(
        hits(&root, &asked),
        [Hit {
            path: "infra/web.yaml".into(),
            line: 2,
            text: "  web-01.example.com:".into(),
        }]
    );
}

#[test]
fn hidden_files_are_searched_and_case_is_ignored() {
    let (_dir, root) = tree();
    let found = paths(&root, &query("WEB-01"));
    assert!(found.contains(&".hidden.yaml".to_owned()));
    assert!(found.contains(&"build/web.yaml".to_owned()));
}

#[test]
fn a_denied_name_is_never_searched_even_when_no_line_ignores_it() {
    let (_dir, root) = tree();
    assert!(!paths(&root, &query("web-01")).contains(&".env".to_owned()));
}

#[test]
fn a_link_is_not_followed() {
    let (_dir, root) = tree();
    symlink("../outside.txt", root.path().join("escape.txt")).unwrap();
    symlink(".env", root.path().join("notes.txt")).unwrap();
    let found = paths(&root, &query("web-01"));
    assert!(!found.contains(&"escape.txt".to_owned()));
    assert!(!found.contains(&"notes.txt".to_owned()));
}

#[test]
fn the_glob_narrows_the_files() {
    let (_dir, root) = tree();
    let mut asked = query("web-01");
    asked.path_glob = Some("infra/**".into());
    assert_eq!(paths(&root, &asked), ["infra/web.yaml"]);
}

#[test]
fn a_fixed_string_is_no_pattern() {
    let (_dir, root) = tree();
    let mut asked = query("web-01.");
    asked.fixed = true;
    assert!(!hits(&root, &asked).is_empty());
    asked.pattern = "web.01".into();
    assert!(hits(&root, &asked).is_empty());
}

#[test]
fn the_limit_counts_lines_across_files() {
    let (_dir, root) = tree();
    let mut asked = query("web-01");
    asked.limit = 2;
    assert_eq!(hits(&root, &asked).len(), 2);
}

#[test]
fn a_binary_file_is_given_up_on() {
    let (_dir, root) = tree();
    fs::write(root.path().join("blob.bin"), b"\0web-01\n").unwrap();
    assert!(!paths(&root, &query("web-01")).contains(&"blob.bin".to_owned()));
}

#[test]
fn a_long_line_is_cut_on_a_character() {
    let (_dir, root) = tree();
    let line = format!("needle{}\n", "\u{e9}".repeat(2000));
    fs::write(root.path().join("long.txt"), line).unwrap();
    let found = hits(&root, &query("needle"));
    assert!(found[0].text.len() <= 1024);
    assert!(found[0].text.starts_with("needle"));
}

#[test]
fn a_bad_pattern_is_an_error() {
    let (_dir, root) = tree();
    let refused = search(&root, &query("("), &mut |_| true);
    assert!(refused.is_err());
}

#[test]
fn a_sink_can_stop_the_search() {
    let (_dir, root) = tree();
    let mut count = 0;
    search(&root, &query("web-01"), &mut |_| {
        count += 1;
        false
    })
    .unwrap();
    assert_eq!(count, 1);
}
