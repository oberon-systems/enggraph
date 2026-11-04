use std::fs;
use std::path::{Path, PathBuf};

use serde_json::json;
use tempfile::TempDir;
use url::Url;
use wiremock::matchers::{body_json, header, method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use super::{apply, operator_token, run, weigh, Listed, Outcome};
use crate::libs::api::HttpApi;
use crate::libs::config::Document;
use crate::libs::exports::Export;

fn canonical(dir: &Path, name: &str) -> String {
    let tree = dir.join(name);
    fs::create_dir_all(&tree).unwrap();
    fs::canonicalize(tree).unwrap().display().to_string()
}

fn listed(name: &str, root_path: &str) -> Listed {
    Listed {
        name: name.into(),
        root_path: root_path.into(),
        bound_to: None,
    }
}

fn document(exports: Vec<Export>) -> Document {
    Document {
        stack_url: "https://stack.example.com".into(),
        name: "alpha".into(),
        labels: Vec::new(),
        state_dir: PathBuf::from("/var/lib/alpha"),
        exports,
    }
}

fn config(dir: &TempDir) -> PathBuf {
    let path = dir.path().join("config.toml");
    let mut empty = document(Vec::new());
    empty.state_dir = dir.path().join("state");
    empty.create(&path).unwrap();
    path
}

fn skipped(outcome: Outcome) -> String {
    match outcome {
        Outcome::Skip(why) => why,
        Outcome::Add => panic!("expected a skip"),
    }
}

#[test]
fn a_directory_on_its_own_path_is_added() {
    let dir = tempfile::tempdir().unwrap();
    let tree = canonical(dir.path(), "beta");

    assert_eq!(
        weigh(&listed("beta", &tree), &document(Vec::new())),
        Outcome::Add
    );
}

#[test]
fn a_path_missing_here_is_skipped_with_the_reason() {
    let why = skipped(weigh(
        &listed("beta", "/nonexistent/beta"),
        &document(Vec::new()),
    ));

    assert!(why.contains("no such directory"), "{why}");
}

#[test]
fn a_link_could_never_bind_so_it_is_skipped() {
    let dir = tempfile::tempdir().unwrap();
    let tree = canonical(dir.path(), "beta");
    let link = dir.path().join("gamma");
    std::os::unix::fs::symlink(&tree, &link).unwrap();
    let link = link.display().to_string();

    let why = skipped(weigh(&listed("gamma", &link), &document(Vec::new())));

    assert!(why.contains("a link to"), "{why}");
}

#[test]
fn a_bound_project_is_not_offered_twice() {
    let dir = tempfile::tempdir().unwrap();
    let tree = canonical(dir.path(), "beta");
    let mut project = listed("beta", &tree);
    project.bound_to = Some("beta".into());

    let why = skipped(weigh(&project, &document(Vec::new())));

    assert!(why.contains("already read through an agent"), "{why}");
}

#[test]
fn a_directory_already_offered_keeps_its_name() {
    let dir = tempfile::tempdir().unwrap();
    let tree = canonical(dir.path(), "beta");
    let held = document(vec![Export {
        name: "delta".into(),
        path: tree.clone(),
    }]);

    let why = skipped(weigh(&listed("beta", &tree), &held));

    assert!(why.contains("already offered as delta"), "{why}");
}

#[test]
fn a_name_taken_by_another_directory_is_skipped() {
    let dir = tempfile::tempdir().unwrap();
    let tree = canonical(dir.path(), "beta");
    let held = document(vec![Export {
        name: "beta".into(),
        path: "/srv/elsewhere".into(),
    }]);

    let why = skipped(weigh(&listed("beta", &tree), &held));

    assert!(why.contains("under this name"), "{why}");
}

#[test]
fn applying_writes_what_it_adds_and_a_second_pass_adds_nothing() {
    let dir = tempfile::tempdir().unwrap();
    let path = config(&dir);
    let tree = canonical(dir.path(), "beta");
    let projects = [listed("beta", &tree), listed("gamma", "/nonexistent/gamma")];

    let first = apply(&path, &projects).unwrap();
    let second = apply(&path, &projects).unwrap();

    assert!(first.contains(&format!("added beta {tree}\n")), "{first}");
    assert!(
        first.contains("skipped gamma /nonexistent/gamma:"),
        "{first}"
    );
    assert!(second.contains("already offered as beta"), "{second}");
    assert_eq!(Document::read(&path).unwrap().exports.len(), 1);
}

#[tokio::test]
async fn the_listing_is_asked_with_the_operator_token() {
    let dir = tempfile::tempdir().unwrap();
    let path_to_config = config(&dir);
    let tree = canonical(dir.path(), "beta");
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/agent/agents/import"))
        .and(header("authorization", "Bearer egt_alpha_beta"))
        .and(body_json(json!({"under": ["/srv"]})))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "projects": [{"name": "beta", "root_path": tree, "bound_to": null}],
        })))
        .expect(1)
        .mount(&server)
        .await;
    let api = HttpApi::new(Url::parse(&server.uri()).unwrap()).unwrap();

    let text = run(
        &path_to_config,
        &api,
        "egt_alpha_beta",
        &["/srv".to_owned()],
    )
    .await
    .unwrap();

    assert!(text.starts_with("added beta "), "{text}");
}

#[tokio::test]
async fn a_token_the_stack_refuses_is_said_and_nothing_is_written() {
    let dir = tempfile::tempdir().unwrap();
    let path_to_config = config(&dir);
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/agent/agents/import"))
        .respond_with(ResponseTemplate::new(403))
        .mount(&server)
        .await;
    let api = HttpApi::new(Url::parse(&server.uri()).unwrap()).unwrap();

    let error = run(&path_to_config, &api, "egt_alpha_beta", &[])
        .await
        .unwrap_err()
        .to_string();

    assert!(error.contains("operator token"), "{error}");
    assert!(!error.contains("egt_alpha_beta"), "{error}");
    assert!(Document::read(&path_to_config).unwrap().exports.is_empty());
}

#[test]
fn a_token_in_the_variable_asks_nothing() {
    assert_eq!(
        operator_token(Some(" egt_alpha_beta\n".into())).unwrap(),
        "egt_alpha_beta"
    );
}
