use std::fs;
use std::path::{Path, PathBuf};

use tempfile::TempDir;

use super::{add, check, check_all, passwd_homes, remove, Export, Kept};
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
    let tree = dir.path().join("beta");
    fs::create_dir(&tree).unwrap();
    add(&path, "beta", &tree).unwrap();

    assert!(add(&path, "beta", &tree)
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
    let entry = Export {
        name: "beta".into(),
        path: "gamma".into(),
    };
    let error = check(&entry, &Kept::default()).unwrap_err().to_string();

    assert!(error.contains("project beta") && error.contains("must be absolute"));
}

#[test]
fn too_many_exports_are_refused() {
    let entry = Export {
        name: "beta".into(),
        path: "/".into(),
    };

    let entries: Vec<Export> = std::iter::repeat_n(entry, 257).collect();

    assert!(check_all(&entries, &Kept::default())
        .unwrap_err()
        .to_string()
        .contains("at most 256"));
}

fn config_with_state(dir: &TempDir, state_dir: &Path, exports: &str) -> PathBuf {
    let path = dir.path().join("etc/config.toml");
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(
        &path,
        format!(
            "stack_url = \"https://stack.example.com\"\nname = \"alpha\"\nstate_dir = {:?}\n{exports}",
            state_dir.display().to_string()
        ),
    )
    .unwrap();
    path
}

#[test]
fn the_root_a_home_and_the_agents_own_directories_are_not_added() {
    let dir = tempfile::tempdir().unwrap();
    let state = dir.path().join("var/state");
    fs::create_dir_all(state.join("keys")).unwrap();
    let path = config_with_state(&dir, &state, "");
    let home = std::env::var_os("HOME").map(PathBuf::from);

    let mut refused = vec![
        PathBuf::from("/"),
        state.clone(),
        state.join("keys"),
        dir.path().join("var"),
        dir.path().join("etc"),
        dir.path().to_path_buf(),
    ];
    refused.extend(home.filter(|home| home.is_dir()));
    for tree in refused {
        let error = add(&path, "beta", &tree).unwrap_err().to_string();

        assert!(
            error.contains("never offered") || error.contains("own state"),
            "{}: {error}",
            tree.display()
        );
    }
    assert!(Document::read(&path).unwrap().exports.is_empty());
}

#[test]
fn a_config_that_lists_what_is_never_offered_does_not_load() {
    let dir = tempfile::tempdir().unwrap();
    let state = dir.path().join("var/state");
    fs::create_dir_all(&state).unwrap();
    let real = fs::canonicalize(dir.path()).unwrap();

    for tree in [PathBuf::from("/"), real.join("var/state"), real.join("etc")] {
        let project = format!(
            "\n[[project]]\nname = \"beta\"\npath = {:?}\n",
            tree.display().to_string()
        );
        let path = config_with_state(&dir, &state, &project);

        assert!(load(&path).is_err(), "{}", tree.display());
    }
}

#[test]
fn a_passwd_file_names_the_homes_of_root_and_of_people() {
    let text = "root:x:0:0:root:/root:/bin/sh\n\
                daemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin\n\
                alpha:x:1000:1000::/home/alpha:/bin/sh\n\
                nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin\n\
                broken line\n";

    assert_eq!(
        passwd_homes(text),
        [PathBuf::from("/root"), PathBuf::from("/home/alpha")]
    );
}

#[test]
fn only_a_home_itself_or_what_holds_it_is_kept_back() {
    let dir = tempfile::tempdir().unwrap();
    let real = fs::canonicalize(dir.path()).unwrap();
    fs::create_dir_all(real.join("home/alpha/src")).unwrap();
    let kept = Kept::new(&real.join("state"), None, &[real.join("home/alpha")]);

    assert!(kept.refusal(&real.join("home")).is_some());
    assert!(kept.refusal(&real.join("home/alpha")).is_some());
    assert!(kept.refusal(&real.join("home/alpha/src")).is_none());
    assert!(kept.refusal(&real.join("home/beta")).is_none());
}
