use std::fs;
use std::path::{Path, PathBuf};

use tempfile::TempDir;

use super::{add, check, check_all, remove, Export};
use crate::libs::config::{load, Document};

fn config(dir: &TempDir) -> PathBuf {
    let path = dir.path().join("config.toml");
    fs::write(
        &path,
        "stack_url = \"https://stack.example.com\"\nname = \"alpha\"\nstate_dir = \"/var/lib/alpha\"\n",
    )
    .unwrap();
    path
}

#[test]
fn an_added_export_is_canonical_and_loads() {
    let dir = tempfile::tempdir().unwrap();
    let path = config(&dir);
    let tree = dir.path().join("beta");
    fs::create_dir(&tree).unwrap();

    let export = add(&path, "beta", &tree.join("../beta")).unwrap();

    let canonical = fs::canonicalize(&tree).unwrap().display().to_string();
    assert_eq!(export.path, canonical);
    assert_eq!(load(&path).unwrap().exports, [export]);
}

#[test]
fn a_name_is_added_only_once() {
    let dir = tempfile::tempdir().unwrap();
    let path = config(&dir);
    add(&path, "beta", dir.path()).unwrap();

    assert!(add(&path, "beta", dir.path())
        .unwrap_err()
        .to_string()
        .contains("twice"));
}

#[test]
fn a_bad_export_leaves_the_file_alone() {
    let dir = tempfile::tempdir().unwrap();
    let path = config(&dir);
    let before = fs::read_to_string(&path).unwrap();

    assert!(add(&path, "beta", Path::new("relative")).is_err());
    assert!(add(&path, "-beta", dir.path()).is_err());

    assert_eq!(fs::read_to_string(&path).unwrap(), before);
}

#[test]
fn an_export_is_removed_even_when_its_directory_is_gone() {
    let dir = tempfile::tempdir().unwrap();
    let path = config(&dir);
    let tree = dir.path().join("beta");
    fs::create_dir(&tree).unwrap();
    add(&path, "beta", &tree).unwrap();
    fs::remove_dir(&tree).unwrap();

    remove(&path, "beta").unwrap();

    assert!(Document::read(&path).unwrap().exports.is_empty());
}

#[test]
fn removing_an_unknown_export_says_so() {
    let dir = tempfile::tempdir().unwrap();
    let path = config(&dir);

    assert!(remove(&path, "beta")
        .unwrap_err()
        .to_string()
        .contains("no project beta"));
}

#[test]
fn a_path_that_is_not_absolute_is_refused_by_name() {
    let error = check(&Export {
        name: "beta".into(),
        path: "gamma".into(),
    })
    .unwrap_err()
    .to_string();

    assert!(error.contains("project beta") && error.contains("must be absolute"));
}

#[test]
fn too_many_exports_are_refused() {
    let entry = Export {
        name: "beta".into(),
        path: "/".into(),
    };

    let entries: Vec<Export> = std::iter::repeat_n(entry, 257).collect();

    assert!(check_all(&entries)
        .unwrap_err()
        .to_string()
        .contains("at most 256"));
}
