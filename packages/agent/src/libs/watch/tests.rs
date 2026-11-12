use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::time::Duration;

use serde_json::{json, Value};
use tempfile::TempDir;
use tokio::sync::mpsc;
use tokio::time::timeout;
use tokio_tungstenite::tungstenite::Message;

use super::Watch;
use crate::libs::exports::Export;

const WAIT: Duration = Duration::from_secs(10);

fn tree() -> (TempDir, Vec<Export>) {
    let dir = TempDir::new().unwrap();
    let root = fs::canonicalize(dir.path()).unwrap().join("alpha");
    fs::create_dir_all(root.join("src")).unwrap();
    fs::create_dir_all(root.join(".git")).unwrap();
    fs::write(root.join("src/app.py"), "one\n").unwrap();
    let export = Export {
        name: "alpha".into(),
        path: root.to_str().unwrap().to_owned(),
    };
    (dir, vec![export])
}

fn root_of(exports: &[Export]) -> std::path::PathBuf {
    std::path::PathBuf::from(&exports[0].path)
}

async fn next(incoming: &mut mpsc::Receiver<Message>) -> Value {
    let message = timeout(WAIT, incoming.recv()).await.unwrap().unwrap();
    serde_json::from_str(message.to_text().unwrap()).unwrap()
}

fn watching(exports: &[Export], skip: &[&str]) -> (Watch, mpsc::Receiver<Message>) {
    let (out, incoming) = mpsc::channel(64);
    let skip: Vec<String> = skip.iter().map(|name| (*name).to_owned()).collect();
    (Watch::start(exports, &skip, out), incoming)
}

async fn changed_until(incoming: &mut mpsc::Receiver<Message>, wanted: &str) {
    loop {
        let frame = next(incoming).await;
        let paths = frame["paths"].as_array().cloned().unwrap_or_default();
        if paths.contains(&json!(wanted)) {
            return;
        }
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn a_watch_owes_a_resync_as_soon_as_it_stands() {
    let (_dir, exports) = tree();
    let (_watch, mut incoming) = watching(&exports, &[]);
    assert_eq!(
        next(&mut incoming).await,
        json!({"type": "resync", "export": "alpha"})
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn a_written_file_is_reported_relative_to_its_export() {
    let (_dir, exports) = tree();
    let (_watch, mut incoming) = watching(&exports, &[]);
    next(&mut incoming).await;

    fs::write(root_of(&exports).join("src/app.py"), "two\n").unwrap();

    changed_until(&mut incoming, "src/app.py").await;
}

#[tokio::test(flavor = "multi_thread")]
async fn a_new_directory_is_watched_and_what_it_holds_reported() {
    let (_dir, exports) = tree();
    let root = root_of(&exports);
    let (_watch, mut incoming) = watching(&exports, &[]);
    next(&mut incoming).await;

    fs::create_dir(root.join("fresh")).unwrap();
    fs::write(root.join("fresh/one.py"), "one\n").unwrap();
    changed_until(&mut incoming, "fresh/one.py").await;

    tokio::time::sleep(Duration::from_millis(1500)).await;
    fs::write(root.join("fresh/two.py"), "two\n").unwrap();
    changed_until(&mut incoming, "fresh/two.py").await;
}

#[tokio::test(flavor = "multi_thread")]
async fn a_skipped_directory_and_a_denied_name_say_nothing() {
    let (_dir, exports) = tree();
    let root = root_of(&exports);
    let (_watch, mut incoming) = watching(&exports, &[".git"]);
    next(&mut incoming).await;

    fs::write(root.join(".git/config"), "x\n").unwrap();
    fs::write(root.join(".env"), "TOKEN=alpha-fake-value\n").unwrap();
    fs::write(root.join("src/app.py"), "two\n").unwrap();

    let frame = next(&mut incoming).await;
    assert_eq!(frame["paths"], json!(["src/app.py"]));
}

#[tokio::test(flavor = "multi_thread")]
async fn an_unreadable_directory_leaves_the_rest_watched() {
    let (_dir, exports) = tree();
    let root = root_of(&exports);
    let locked = root.join("locked");
    fs::create_dir(&locked).unwrap();
    fs::set_permissions(&locked, fs::Permissions::from_mode(0o000)).unwrap();
    // Root reads through any mode, and then there is nothing to skip.
    if fs::read_dir(&locked).is_ok() {
        fs::set_permissions(&locked, fs::Permissions::from_mode(0o755)).unwrap();
        return;
    }
    let (_watch, mut incoming) = watching(&exports, &[]);
    next(&mut incoming).await;

    fs::write(root.join("src/app.py"), "two\n").unwrap();

    changed_until(&mut incoming, "src/app.py").await;
    fs::set_permissions(&locked, fs::Permissions::from_mode(0o755)).unwrap();
}

#[tokio::test(flavor = "multi_thread")]
async fn a_dropped_watch_goes_quiet() {
    let (_dir, exports) = tree();
    let (watch, mut incoming) = watching(&exports, &[]);
    next(&mut incoming).await;

    drop(watch);

    assert!(timeout(WAIT, incoming.recv()).await.unwrap().is_none());
}

#[tokio::test(flavor = "multi_thread")]
async fn a_directory_that_is_a_link_out_is_neither_watched_nor_reported() {
    let (dir, exports) = tree();
    let root = root_of(&exports);
    let away = dir.path().join("away");
    fs::create_dir(&away).unwrap();
    let (_watch, mut incoming) = watching(&exports, &[]);
    next(&mut incoming).await;

    std::os::unix::fs::symlink(&away, root.join("linked")).unwrap();
    tokio::time::sleep(Duration::from_millis(1500)).await;
    fs::write(away.join("theirs.txt"), "x\n").unwrap();
    fs::write(root.join("src/app.py"), "two\n").unwrap();

    loop {
        let frame = next(&mut incoming).await;
        let paths = frame["paths"].as_array().cloned().unwrap_or_default();
        assert!(!paths.contains(&json!("linked/theirs.txt")));
        if paths.contains(&json!("src/app.py")) {
            break;
        }
    }
}
