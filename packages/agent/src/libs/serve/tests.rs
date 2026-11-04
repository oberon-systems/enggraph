use std::fs;
use std::io::Read;
use std::time::Duration;

use serde_json::{json, Value};
use tempfile::TempDir;
use tokio::sync::mpsc;
use tokio::time::timeout;
use tokio_tungstenite::tungstenite::Message;

use super::Server;
use crate::libs::exports::Export;
use crate::libs::protocol::{Limits, Request};
use crate::libs::walk::text_md5;

const WAIT: Duration = Duration::from_secs(10);

struct Stand {
    _dir: TempDir,
    server: Server,
    incoming: mpsc::Receiver<Message>,
}

fn stand_with(limits: Limits) -> Stand {
    let dir = TempDir::new().unwrap();
    let base = fs::canonicalize(dir.path()).unwrap();
    let root = base.join("alpha");
    fs::create_dir_all(root.join("src")).unwrap();
    fs::write(root.join("src/app.py"), "import os\nweb-01\n").unwrap();
    fs::write(root.join(".env"), "TOKEN=alpha-fake-value\n").unwrap();
    fs::write(base.join("outside.txt"), "not yours").unwrap();
    let exports = [Export {
        name: "alpha".into(),
        path: root.to_str().unwrap().to_owned(),
    }];
    let (out, incoming) = mpsc::channel(1024);
    Stand {
        _dir: dir,
        server: Server::new(&exports, limits, out),
        incoming,
    }
}

fn stand() -> Stand {
    stand_with(Limits::default())
}

fn ask(stand: &Stand, id: u32, op: &str, args: Value) {
    stand.server.dispatch(Request {
        id,
        op: op.into(),
        args,
    });
}

async fn next(stand: &mut Stand) -> Message {
    timeout(WAIT, stand.incoming.recv()).await.unwrap().unwrap()
}

async fn answer(stand: &mut Stand) -> Value {
    let message = next(stand).await;
    serde_json::from_str(message.to_text().unwrap()).unwrap()
}

async fn bytes(stand: &mut Stand, id: u32) -> Vec<u8> {
    let mut found = Vec::new();
    loop {
        let Message::Binary(frame) = next(stand).await else {
            panic!("expected a binary frame");
        };
        assert_eq!(frame[..4], id.to_be_bytes());
        found.extend_from_slice(&frame[5..]);
        if frame[4] == 1 {
            return found;
        }
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn an_unknown_op_is_refused() {
    let mut stand = stand();
    ask(&stand, 1, "write", json!({"export": "alpha", "path": "x"}));
    let frame = answer(&mut stand).await;
    assert_eq!(frame["ok"], json!(false));
    assert_eq!(frame["error"]["code"], json!("unknown_op"));
}

#[tokio::test(flavor = "multi_thread")]
async fn an_unknown_export_is_refused() {
    let mut stand = stand();
    ask(&stand, 2, "stat", json!({"export": "beta", "paths": ["x"]}));
    assert_eq!(
        answer(&mut stand).await["error"]["code"],
        json!("unknown_export")
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn an_argument_nobody_reads_is_refused() {
    let mut stand = stand();
    ask(
        &stand,
        3,
        "read",
        json!({"export": "alpha", "path": "x", "mode": "w"}),
    );
    assert_eq!(answer(&mut stand).await["error"]["code"], json!("bad_args"));
}

#[tokio::test(flavor = "multi_thread")]
async fn exports_can_change_under_a_running_server() {
    let mut stand = stand();
    stand.server.set_exports(&[]);
    ask(
        &stand,
        3,
        "stat",
        json!({"export": "alpha", "paths": ["x"]}),
    );
    assert_eq!(
        answer(&mut stand).await["error"]["code"],
        json!("unknown_export")
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn a_walk_hashes_by_default_and_ends_without_more() {
    let mut stand = stand();
    ask(&stand, 4, "walk", json!({"export": "alpha"}));
    let frame = answer(&mut stand).await;
    assert_eq!(frame["id"], json!(4));
    assert_eq!(frame.get("more"), None);
    let entries = frame["result"]["entries"].as_array().unwrap();
    let paths: Vec<&str> = entries
        .iter()
        .map(|e| e["path"].as_str().unwrap())
        .collect();
    assert_eq!(paths, [".env", "src/app.py"]);
    assert_eq!(entries[0]["md5"], json!(null));
    assert_eq!(entries[1]["md5"], json!(text_md5(b"import os\nweb-01\n")));
}

#[tokio::test(flavor = "multi_thread")]
async fn a_shallow_walk_names_the_directories_to_go_on_with() {
    let mut stand = stand();
    ask(
        &stand,
        5,
        "walk",
        json!({"export": "alpha", "deep": false, "md5": false}),
    );
    let frame = answer(&mut stand).await;
    assert_eq!(frame["result"]["dirs"], json!(["src"]));
    assert_eq!(frame["result"]["entries"][0]["md5"], json!(null));
}

#[tokio::test(flavor = "multi_thread")]
async fn a_large_walk_comes_in_batches() {
    let mut stand = stand();
    let root = stand.server.roots.read().unwrap()["alpha"].clone();
    for number in 0..1100 {
        fs::write(root.path().join(format!("f{number:04}.txt")), "x").unwrap();
    }
    ask(&stand, 6, "walk", json!({"export": "alpha", "md5": false}));
    let mut seen = 0;
    let mut frames = 0;
    loop {
        let frame = answer(&mut stand).await;
        frames += 1;
        seen += frame["result"]["entries"].as_array().unwrap().len();
        if frame.get("more").is_none() {
            break;
        }
        assert_eq!(frame["more"], json!(true));
    }
    assert!(frames >= 3);
    assert_eq!(seen, 1102);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_stat_says_what_a_path_is() {
    let mut stand = stand();
    let paths = ["src/app.py", "src", "gone", "../outside.txt"];
    ask(
        &stand,
        7,
        "stat",
        json!({"export": "alpha", "paths": paths}),
    );
    let frame = answer(&mut stand).await;
    let stats = frame["result"]["stats"].as_array().unwrap();
    assert_eq!(
        stats[0],
        json!({"path": "src/app.py", "is_file": true, "is_dir": false, "size": 17, "contained": true})
    );
    assert_eq!(stats[1]["is_dir"], json!(true));
    assert_eq!(stats[2]["contained"], json!(true));
    assert_eq!(stats[2]["size"], json!(null));
    assert_eq!(stats[3]["contained"], json!(false));
    assert_eq!(stats[3]["is_file"], json!(false));
}

#[tokio::test(flavor = "multi_thread")]
async fn a_read_comes_in_data_frames() {
    let mut stand = stand();
    ask(
        &stand,
        8,
        "read",
        json!({"export": "alpha", "path": "src/app.py"}),
    );
    assert_eq!(bytes(&mut stand, 8).await, b"import os\nweb-01\n");
}

#[tokio::test(flavor = "multi_thread")]
async fn a_large_read_comes_in_chunks_and_an_empty_one_still_ends() {
    let mut stand = stand_with(Limits {
        max_file_bytes: 10_000_000,
        skip_dirs: Vec::new(),
    });
    let root = stand.server.roots.read().unwrap()["alpha"].clone();
    fs::write(root.path().join("big.bin"), vec![7_u8; 600_000]).unwrap();
    fs::write(root.path().join("empty.txt"), "").unwrap();

    ask(
        &stand,
        9,
        "read",
        json!({"export": "alpha", "path": "big.bin"}),
    );
    assert_eq!(bytes(&mut stand, 9).await.len(), 600_000);

    ask(
        &stand,
        10,
        "read",
        json!({"export": "alpha", "path": "empty.txt"}),
    );
    assert_eq!(bytes(&mut stand, 10).await, b"");
}

#[tokio::test(flavor = "multi_thread")]
async fn a_read_refused_says_why() {
    let mut stand = stand_with(Limits {
        max_file_bytes: 5,
        skip_dirs: Vec::new(),
    });
    for (id, path, code) in [
        (11, ".env", "denied"),
        (12, "src/app.py", "over the size limit"),
        (13, "gone.py", "unreadable"),
        (14, "../outside.txt", "outside"),
    ] {
        ask(&stand, id, "read", json!({"export": "alpha", "path": path}));
        let frame = answer(&mut stand).await;
        assert_eq!(frame["error"]["code"], json!(code), "{path}");
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn a_head_gives_the_first_bytes() {
    let mut stand = stand();
    ask(
        &stand,
        15,
        "head",
        json!({"export": "alpha", "path": "src/app.py", "n": 6}),
    );
    assert_eq!(bytes(&mut stand, 15).await, b"import");
}

#[tokio::test(flavor = "multi_thread")]
async fn a_grep_answers_lines_and_never_a_denied_file() {
    let mut stand = stand();
    let root = stand.server.roots.read().unwrap()["alpha"].clone();
    fs::write(root.path().join(".env"), "HOST=web-01\n").unwrap();
    ask(
        &stand,
        16,
        "grep",
        json!({"export": "alpha", "pattern": "web-01", "ignore_lines": [], "limit": 10}),
    );
    let frame = answer(&mut stand).await;
    assert_eq!(
        frame["result"]["matches"],
        json!([{"path": "src/app.py", "line": 2, "text": "web-01"}])
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn a_bad_pattern_is_refused() {
    let mut stand = stand();
    ask(
        &stand,
        17,
        "grep",
        json!({"export": "alpha", "pattern": "(", "limit": 1}),
    );
    assert_eq!(answer(&mut stand).await["error"]["code"], json!("bad_args"));
}

#[tokio::test(flavor = "multi_thread")]
async fn a_fetch_is_a_tar_of_what_may_be_read() {
    let mut stand = stand();
    let paths = ["src/app.py", ".env", "../outside.txt"];
    ask(
        &stand,
        18,
        "fetch",
        json!({"export": "alpha", "paths": paths}),
    );
    let archive = bytes(&mut stand, 18).await;
    let mut reader = tar::Archive::new(archive.as_slice());
    let mut found = Vec::new();
    for entry in reader.entries().unwrap() {
        let mut entry = entry.unwrap();
        let mut text = String::new();
        entry.read_to_string(&mut text).unwrap();
        found.push((entry.path().unwrap().display().to_string(), text));
    }
    assert_eq!(
        found,
        [("src/app.py".to_owned(), "import os\nweb-01\n".to_owned())]
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn past_the_cap_a_request_is_busy() {
    let mut stand = stand();
    let permits = stand.server.slots.available_permits();
    let held = stand
        .server
        .slots
        .clone()
        .try_acquire_many_owned(permits as u32)
        .unwrap();
    ask(&stand, 19, "stat", json!({"export": "alpha", "paths": []}));
    assert_eq!(answer(&mut stand).await["error"]["code"], json!("busy"));
    drop(held);
    ask(&stand, 20, "stat", json!({"export": "alpha", "paths": []}));
    assert_eq!(answer(&mut stand).await["ok"], json!(true));
}

#[tokio::test(flavor = "multi_thread")]
async fn past_the_budget_a_request_is_busy() {
    let mut stand = stand();
    stand.server.window.lock().unwrap().1 = super::MAX_PER_MINUTE;
    ask(&stand, 21, "stat", json!({"export": "alpha", "paths": []}));
    assert_eq!(answer(&mut stand).await["error"]["code"], json!("busy"));
}
