use std::fs;
use std::os::unix::fs::symlink;
use std::path::Path;

use rustix::fs::{Mode, OFlags};
use tempfile::TempDir;

use super::{is_denied, relative, Refusal, Root, Unread, DENIED_NAMES};

const CONFIG: &str = include_str!("../../../../core/src/enggraph/core/config.py");
const MAX: u64 = 1_000;

struct Tree {
    _dir: TempDir,
    root: Root,
    outside: std::path::PathBuf,
}

fn tree() -> Tree {
    let dir = TempDir::new().unwrap();
    let base = fs::canonicalize(dir.path()).unwrap();
    let root = base.join("alpha");
    fs::create_dir_all(root.join("src")).unwrap();
    fs::write(root.join("src/app.py"), "import os\n").unwrap();
    fs::write(root.join(".env"), "TOKEN=alpha-fake-value\n").unwrap();
    let outside = base.join("outside.txt");
    fs::write(&outside, "not yours").unwrap();
    Tree {
        _dir: dir,
        root: Root::new(root),
        outside,
    }
}

fn denied_names_in_config() -> Vec<String> {
    let (_, rest) = CONFIG.split_once("CONTENT_DENIED_NAMES = (").unwrap();
    let (body, _) = rest.split_once("\n)").unwrap();
    body.lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .map(|line| line.trim_end_matches(',').trim_matches('"').to_owned())
        .collect()
}

#[test]
fn the_denied_names_are_the_ones_the_stack_holds() {
    let mut ours: Vec<String> = DENIED_NAMES.iter().map(|name| (*name).to_owned()).collect();
    let mut stack = denied_names_in_config();
    ours.sort();
    stack.sort();
    assert_eq!(ours, stack);
}

#[test]
fn the_denied_names_match_as_fnmatch_does() {
    for name in [
        ".env",
        ".env.production",
        ".env.",
        "server.pem",
        ".pem",
        "id_rsa",
        "id_rsa.pub",
        "id_ed25519",
        "credentials",
        "authorized_keys",
        "alpha.tfvars",
    ] {
        assert!(is_denied(name), "{name}");
    }
    for name in ["env", "alpha.env", "pem", "my_id_rsa", "credentials.txt"] {
        assert!(!is_denied(name), "{name}");
    }
}

#[test]
fn a_path_may_not_be_absolute_or_climb() {
    for raw in [
        "/etc/passwd",
        "../outside.txt",
        "src/../../outside.txt",
        "a\0b",
    ] {
        assert!(matches!(relative(raw), Err(Refusal::Outside)), "{raw}");
    }
    assert_eq!(relative("./src//app.py").unwrap(), Path::new("src/app.py"));
    assert_eq!(relative("").unwrap(), Path::new(""));
}

#[test]
fn a_file_of_the_tree_is_read() {
    let tree = tree();
    assert_eq!(tree.root.read("src/app.py", MAX).unwrap(), b"import os\n");
    assert_eq!(tree.root.head("src/app.py", 6).unwrap(), b"import");
}

#[test]
fn traversal_and_absolute_paths_are_refused() {
    let tree = tree();
    let outside = tree.outside.to_str().unwrap();
    assert_eq!(tree.root.read("../outside.txt", MAX), Err(Unread::Outside));
    assert_eq!(tree.root.read(outside, MAX), Err(Unread::Outside));
    assert!(!tree.root.contains("../outside.txt"));
    assert!(!tree.root.contains(outside));
}

#[test]
fn a_link_leaving_the_root_is_refused() {
    let tree = tree();
    symlink(&tree.outside, tree.root.path().join("absolute.txt")).unwrap();
    symlink("../outside.txt", tree.root.path().join("climbing.txt")).unwrap();
    for name in ["absolute.txt", "climbing.txt"] {
        assert_eq!(tree.root.read(name, MAX), Err(Unread::Outside), "{name}");
        assert!(!tree.root.contains(name), "{name}");
    }
}

#[test]
fn a_directory_linked_out_is_refused_for_what_lies_below_it() {
    let tree = tree();
    let parent = tree.outside.parent().unwrap().to_owned();
    symlink(parent, tree.root.path().join("linked")).unwrap();
    assert_eq!(
        tree.root.read("linked/outside.txt", MAX),
        Err(Unread::Outside)
    );
}

#[test]
fn a_magic_link_is_refused() {
    let tree = tree();
    symlink(
        "/proc/self/root/etc/hostname",
        tree.root.path().join("magic"),
    )
    .unwrap();
    assert_eq!(tree.root.read("magic", MAX), Err(Unread::Outside));
}

#[test]
fn a_link_inside_the_root_is_followed() {
    let tree = tree();
    symlink("src/app.py", tree.root.path().join("linked.py")).unwrap();
    assert_eq!(tree.root.read("linked.py", MAX).unwrap(), b"import os\n");
    assert!(tree.root.contains("linked.py"));
}

#[test]
fn a_missing_path_is_inside_but_unreadable() {
    let tree = tree();
    assert!(tree.root.contains("src/gone.py"));
    assert_eq!(tree.root.read("src/gone.py", MAX), Err(Unread::Unreadable));
}

#[test]
fn a_denied_name_is_never_read() {
    let tree = tree();
    assert_eq!(tree.root.read(".env", MAX), Err(Unread::Denied));
    assert_eq!(tree.root.head(".env", 10), Err(Unread::Denied));
}

#[test]
fn a_link_to_a_denied_name_is_never_read() {
    let tree = tree();
    symlink(".env", tree.root.path().join("notes.txt")).unwrap();
    assert_eq!(tree.root.read("notes.txt", MAX), Err(Unread::Denied));
    assert_eq!(tree.root.head("notes.txt", 10), Err(Unread::Denied));
}

#[test]
fn a_file_over_the_limit_is_refused_but_its_head_is_not() {
    let tree = tree();
    fs::write(tree.root.path().join("big.txt"), "x".repeat(11)).unwrap();
    assert_eq!(tree.root.read("big.txt", 10), Err(Unread::Oversized));
    assert_eq!(tree.root.read("big.txt", 11).unwrap().len(), 11);
    assert_eq!(tree.root.head("big.txt", 2).unwrap(), b"xx");
}

#[test]
fn a_directory_or_a_fifo_is_unreadable_and_does_not_hang() {
    let tree = tree();
    rustix::fs::mknodat(
        rustix::fs::CWD,
        tree.root.path().join("pipe"),
        rustix::fs::FileType::Fifo,
        Mode::RUSR | Mode::WUSR,
        0,
    )
    .unwrap();
    assert_eq!(tree.root.read("pipe", MAX), Err(Unread::Unreadable));
    assert_eq!(tree.root.read("src", MAX), Err(Unread::Unreadable));
}

#[test]
fn the_fallback_refuses_what_openat2_refuses() {
    let tree = tree();
    symlink(&tree.outside, tree.root.path().join("escape.txt")).unwrap();
    let flags = OFlags::RDONLY | OFlags::CLOEXEC;
    assert!(matches!(
        tree.root.fallback(Path::new("escape.txt"), flags),
        Err(Refusal::Outside)
    ));
    assert!(tree.root.fallback(Path::new("src/app.py"), flags).is_ok());
}

#[test]
fn a_directory_is_opened_only_when_it_is_no_link() {
    let tree = tree();
    symlink("src", tree.root.path().join("linked")).unwrap();
    assert!(tree.root.open_dir("src").is_ok());
    assert!(tree.root.open_dir("").is_ok());
    assert!(tree.root.open_dir("linked").is_err());
}

#[test]
fn the_fallback_follows_a_link_only_while_it_stays_inside() {
    let tree = tree();
    let flags = OFlags::RDONLY | OFlags::CLOEXEC;
    symlink("src/app.py", tree.root.path().join("inside.py")).unwrap();
    assert!(tree.root.fallback(Path::new("inside.py"), flags).is_ok());
    symlink("../src", tree.root.path().join("src/again")).unwrap();
    assert!(tree
        .root
        .fallback(Path::new("src/again/app.py"), flags)
        .is_ok());
    symlink("../..", tree.root.path().join("src/up")).unwrap();
    assert!(matches!(
        tree.root.fallback(Path::new("src/up/anything"), flags),
        Err(Refusal::Outside)
    ));
}

#[test]
fn the_fallback_does_not_go_through_a_parent_that_is_a_link_out() {
    let tree = tree();
    let flags = OFlags::RDONLY | OFlags::CLOEXEC;
    let away = tree.outside.parent().unwrap().to_owned();
    symlink(&away, tree.root.path().join("swapped")).unwrap();
    let name = tree.outside.file_name().unwrap();
    assert!(matches!(
        tree.root.fallback(&Path::new("swapped").join(name), flags),
        Err(Refusal::Outside)
    ));
}

#[test]
fn the_fallback_answers_a_link_itself_only_to_lstat() {
    let tree = tree();
    symlink("src", tree.root.path().join("linked")).unwrap();
    let look = OFlags::PATH | OFlags::CLOEXEC | OFlags::NOFOLLOW;
    let seen = tree.root.fallback(Path::new("linked"), look).unwrap();
    assert!(seen.metadata().unwrap().file_type().is_symlink());
    let list = OFlags::RDONLY | OFlags::CLOEXEC | OFlags::DIRECTORY | OFlags::NOFOLLOW;
    assert!(tree.root.fallback(Path::new("linked"), list).is_err());
    assert!(tree.root.fallback(Path::new(""), list).is_ok());
}
