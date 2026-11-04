use std::fs;
use std::os::unix::fs::PermissionsExt;

use super::{Credential, State, StateStore};
use crate::libs::exports::Reported;

fn state() -> State {
    State {
        agent_id: Some("alpha".into()),
        claim_code: None,
        credential: Some(Credential {
            header: "k1:1:1893456000".into(),
            expires_at: 1_893_456_000,
            key: "beta".into(),
        }),
        exports: vec![Reported {
            name: "gamma".into(),
            host_path: "/srv/gamma".into(),
            state: "approved".into(),
            bound_to: None,
        }],
    }
}

#[test]
fn a_missing_file_is_an_empty_state() {
    let dir = tempfile::tempdir().unwrap();

    assert_eq!(
        StateStore::new(dir.path()).load().unwrap(),
        State::default()
    );
}

#[test]
fn a_saved_state_reads_back_from_a_private_file() {
    let dir = tempfile::tempdir().unwrap();
    let store = StateStore::new(dir.path());

    store.save(&state()).unwrap();

    assert_eq!(store.load().unwrap(), state());
    let mode = fs::metadata(dir.path().join("state.json"))
        .unwrap()
        .permissions()
        .mode();
    assert_eq!(mode & 0o777, 0o600);
}

#[test]
fn a_save_leaves_nothing_beside_the_state() {
    let dir = tempfile::tempdir().unwrap();
    let store = StateStore::new(dir.path());

    store.save(&state()).unwrap();
    store.save(&state()).unwrap();

    let names: Vec<_> = fs::read_dir(dir.path())
        .unwrap()
        .map(|entry| entry.unwrap().file_name())
        .collect();
    assert_eq!(names, ["state.json"]);
}

#[test]
fn a_state_file_others_can_read_is_refused() {
    let dir = tempfile::tempdir().unwrap();
    let store = StateStore::new(dir.path());
    store.save(&state()).unwrap();
    let path = dir.path().join("state.json");
    fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).unwrap();

    assert!(store.load().is_err());
}

#[test]
fn a_malformed_state_file_names_only_itself() {
    let dir = tempfile::tempdir().unwrap();
    let store = StateStore::new(dir.path());
    store.save(&state()).unwrap();
    let path = dir.path().join("state.json");
    fs::write(&path, "{\"key\": delta").unwrap();

    let error = store.load().unwrap_err().to_string();

    assert!(error.contains("malformed"));
    assert!(!error.contains("delta"));
}

#[test]
fn secrets_never_reach_debug_output() {
    let mut state = state();
    state.claim_code = Some("delta".into());

    let shown = format!("{state:?}");

    assert!(!shown.contains("beta"));
    assert!(!shown.contains("delta"));
}

#[test]
fn a_credential_reads_from_the_answer_of_the_stack_and_from_the_state_file() {
    let answered: Credential =
        serde_json::from_str(r#"{"credential":"k1:1:1","expires_at":1,"key":"beta"}"#).unwrap();
    let kept: Credential =
        serde_json::from_str(r#"{"header":"k1:1:1","expires_at":1,"key":"beta"}"#).unwrap();

    assert_eq!(answered, kept);
    assert_eq!(answered.header, "k1:1:1");
}

#[test]
fn prepare_makes_a_private_directory() {
    let dir = tempfile::tempdir().unwrap();
    let state_dir = dir.path().join("alpha/beta");
    let store = StateStore::new(&state_dir);

    store.prepare().unwrap();
    store.prepare().unwrap();

    let mode = fs::metadata(&state_dir).unwrap().permissions().mode();
    assert_eq!(mode & 0o777, 0o700);
}

#[test]
fn a_second_agent_on_the_same_state_is_refused() {
    let dir = tempfile::tempdir().unwrap();
    let store = StateStore::new(dir.path());

    let held = store.lock().unwrap();

    assert!(store
        .lock()
        .unwrap_err()
        .to_string()
        .contains("another agent"));
    drop(held);
    assert!(store.lock().is_ok());
}
