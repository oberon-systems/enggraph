use std::fs;
use std::os::unix::fs::{symlink, PermissionsExt};

use serde::Deserialize;
use tempfile::TempDir;

use super::{decoded, list, text_md5, walk, Listing};
use crate::libs::confine::Root;

const VECTORS: &str = include_str!("../../../../core/tests/data/tree_hash_vectors.json");
const MAX: u64 = 1_000;

#[derive(Deserialize)]
struct Vector {
    name: String,
    bytes_hex: String,
    text_hex: String,
    md5: String,
}

fn tree() -> (TempDir, Root) {
    let dir = TempDir::new().unwrap();
    let base = fs::canonicalize(dir.path()).unwrap();
    let root = base.join("alpha");
    fs::create_dir_all(root.join("b/deep")).unwrap();
    fs::create_dir_all(root.join("a")).unwrap();
    fs::write(root.join("z.py"), "z\n").unwrap();
    fs::write(root.join("c.py"), "c\n").unwrap();
    fs::write(root.join("a/one.py"), "one\n").unwrap();
    fs::write(root.join("b/two.py"), "two\n").unwrap();
    fs::write(root.join("b/deep/three.py"), "three\n").unwrap();
    fs::write(base.join("outside.txt"), "not yours").unwrap();
    (dir, Root::new(root))
}

fn paths(listing: &Listing) -> Vec<&str> {
    listing
        .entries
        .iter()
        .map(|entry| entry.path.as_str())
        .collect()
}

#[test]
fn the_hash_is_the_one_the_stack_computes() {
    let vectors: Vec<Vector> = serde_json::from_str(VECTORS).unwrap();
    assert!(!vectors.is_empty());
    for vector in vectors {
        let bytes = hex::decode(&vector.bytes_hex).unwrap();
        assert_eq!(
            hex::encode(decoded(&bytes)),
            vector.text_hex,
            "{}",
            vector.name
        );
        assert_eq!(text_md5(&bytes), vector.md5, "{}", vector.name);
    }
}

#[test]
fn a_directory_lists_its_files_and_directories_sorted() {
    let (_dir, root) = tree();
    let listing = list(&root, "", MAX, false);
    assert_eq!(paths(&listing), ["c.py", "z.py"]);
    assert_eq!(listing.dirs, ["a", "b"]);
    assert!(listing.skipped.is_empty());
    assert_eq!(listing.entries[0].size, 2);
    assert_eq!(listing.entries[0].md5, None);
}

#[test]
fn a_walk_goes_as_os_walk_does() {
    let (_dir, root) = tree();
    let mut seen = Vec::new();
    walk(&root, "", MAX, false, &mut |listing| {
        seen.extend(listing.entries.into_iter().map(|entry| entry.path));
        true
    });
    assert_eq!(
        seen,
        ["c.py", "z.py", "a/one.py", "b/two.py", "b/deep/three.py"]
    );
}

#[test]
fn a_sink_can_stop_a_walk() {
    let (_dir, root) = tree();
    let mut calls = 0;
    walk(&root, "", MAX, false, &mut |_| {
        calls += 1;
        false
    });
    assert_eq!(calls, 1);
}

#[test]
fn a_listing_hashes_on_request() {
    let (_dir, root) = tree();
    let listing = list(&root, "a", MAX, true);
    assert_eq!(listing.entries[0].md5, Some(text_md5(b"one\n")));
}

#[test]
fn a_denied_file_is_not_listed_at_all() {
    let (_dir, root) = tree();
    fs::write(root.path().join(".env"), "TOKEN=alpha-fake-value\n").unwrap();
    fs::create_dir(root.path().join("credentials")).unwrap();
    let listing = list(&root, "", MAX, true);
    assert!(listing.entries.iter().all(|entry| entry.path != ".env"));
    // A directory is walked whatever it is called: the rule is about files.
    assert!(listing.dirs.contains(&"credentials".to_owned()));
}

#[test]
fn a_file_over_the_limit_is_listed_with_no_hash() {
    let (_dir, root) = tree();
    fs::write(root.path().join("big.txt"), "x".repeat(20)).unwrap();
    let listing = list(&root, "", 10, true);
    let big = listing
        .entries
        .iter()
        .find(|one| one.path == "big.txt")
        .unwrap();
    assert_eq!(big.md5, None);
    assert_eq!(big.size, 20);
}

#[test]
fn a_linked_directory_is_neither_listed_nor_descended() {
    let (_dir, root) = tree();
    symlink("a", root.path().join("linked")).unwrap();
    let listing = list(&root, "", MAX, false);
    assert!(!paths(&listing).contains(&"linked"));
    assert!(!listing.dirs.contains(&"linked".to_owned()));
}

#[test]
fn a_link_out_of_the_root_is_listed_with_no_hash() {
    let (_dir, root) = tree();
    symlink("../outside.txt", root.path().join("escape.txt")).unwrap();
    let listing = list(&root, "", MAX, true);
    let escape = listing
        .entries
        .iter()
        .find(|one| one.path == "escape.txt")
        .unwrap();
    assert_eq!(escape.md5, None);
}

#[test]
fn a_linked_file_inside_the_root_is_listed_and_hashed() {
    let (_dir, root) = tree();
    symlink("c.py", root.path().join("linked.py")).unwrap();
    let listing = list(&root, "", MAX, true);
    let linked = listing
        .entries
        .iter()
        .find(|one| one.path == "linked.py")
        .unwrap();
    assert_eq!(linked.md5, Some(text_md5(b"c\n")));
}

#[test]
fn a_missing_top_lists_as_empty() {
    let (_dir, root) = tree();
    assert_eq!(list(&root, "gone", MAX, false), Listing::default());
}

#[test]
fn a_top_that_climbs_out_lists_nothing() {
    let (_dir, root) = tree();
    let listing = list(&root, "..", MAX, false);
    assert!(listing.entries.is_empty() && listing.dirs.is_empty());
}

#[test]
fn an_unreadable_directory_is_skipped_and_reported() {
    let (_dir, root) = tree();
    let locked = root.path().join("a");
    fs::set_permissions(&locked, fs::Permissions::from_mode(0o000)).unwrap();
    // Root reads through any mode, and then there is nothing to report.
    if fs::read_dir(&locked).is_ok() {
        fs::set_permissions(&locked, fs::Permissions::from_mode(0o755)).unwrap();
        return;
    }
    let mut seen = Vec::new();
    let mut skipped = Vec::new();
    walk(&root, "", MAX, false, &mut |listing| {
        seen.extend(listing.entries.into_iter().map(|entry| entry.path));
        skipped.extend(listing.skipped.into_iter().map(|one| one.path));
        true
    });
    fs::set_permissions(&locked, fs::Permissions::from_mode(0o755)).unwrap();
    assert_eq!(skipped, ["a"]);
    assert!(seen.contains(&"b/two.py".to_owned()));
}
