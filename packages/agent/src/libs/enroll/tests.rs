use std::collections::VecDeque;
use std::fs;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use serde_json::json;
use tempfile::TempDir;
use tokio::net::TcpListener;
use tokio::time::timeout;
use tokio_tungstenite::tungstenite::handshake::server::{Request, Response};
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;
use tokio_tungstenite::tungstenite::protocol::CloseFrame;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::{accept_hdr_async, WebSocketStream};
use url::Url;

use super::{supervise, Agent, Step, ANNOUNCE_EVERY, ANNOUNCE_UNSETTLED, CLAIM_POLL};
use crate::libs::api::{unix_now, Api, Claim, Signed};
use crate::libs::config::Config;
use crate::libs::error::AgentError;
use crate::libs::exports::{Export, Reported};
use crate::libs::state::{Credential, State, StateStore};

const WAIT: Duration = Duration::from_secs(20);

#[derive(Default)]
struct Calls {
    registered: Mutex<Vec<Option<String>>>,
    registered_exports: Mutex<Vec<Vec<Export>>>,
    claims: Mutex<VecDeque<Claim>>,
    refreshes: Mutex<VecDeque<Result<Signed<Credential>, AgentError>>>,
    announcements: Mutex<VecDeque<Result<Signed<Vec<Reported>>, AgentError>>>,
    announced: Mutex<Vec<Vec<Export>>>,
    signed_with: Mutex<Vec<String>>,
}

#[derive(Clone, Default)]
struct FakeApi(Arc<Calls>);

impl Api for FakeApi {
    async fn register(
        &self,
        _name: &str,
        _labels: &[String],
        agent_id: Option<&str>,
        exports: &[Export],
    ) -> Result<String, AgentError> {
        self.0
            .registered
            .lock()
            .unwrap()
            .push(agent_id.map(str::to_owned));
        self.0
            .registered_exports
            .lock()
            .unwrap()
            .push(exports.to_vec());
        Ok("delta".into())
    }

    async fn claim(&self, _code: &str) -> Result<Claim, AgentError> {
        Ok(self
            .0
            .claims
            .lock()
            .unwrap()
            .pop_front()
            .unwrap_or(Claim::Waiting))
    }

    async fn refresh(
        &self,
        _agent_id: &str,
        credential: &Credential,
    ) -> Result<Signed<Credential>, AgentError> {
        self.0
            .signed_with
            .lock()
            .unwrap()
            .push(credential.header.clone());
        self.0
            .refreshes
            .lock()
            .unwrap()
            .pop_front()
            .unwrap_or(Ok(Signed::Refused))
    }

    async fn put_exports(
        &self,
        _agent_id: &str,
        credential: &Credential,
        exports: &[Export],
    ) -> Result<Signed<Vec<Reported>>, AgentError> {
        self.0
            .signed_with
            .lock()
            .unwrap()
            .push(credential.header.clone());
        self.0.announced.lock().unwrap().push(exports.to_vec());
        self.0
            .announcements
            .lock()
            .unwrap()
            .pop_front()
            .unwrap_or(Ok(Signed::Accepted(Vec::new())))
    }
}

fn credential(header: &str, expires_in: i64) -> Credential {
    Credential {
        header: header.into(),
        expires_at: unix_now() + expires_in,
        key: "beta".into(),
    }
}

fn enrolled(expires_in: i64) -> State {
    State {
        agent_id: Some("gamma".into()),
        credential: Some(credential("k1:1:1", expires_in)),
        ..State::default()
    }
}

fn config(dir: &TempDir, stack: &Url) -> Config {
    Config {
        stack_url: stack.clone(),
        name: "alpha".into(),
        labels: Vec::new(),
        state_dir: dir.path().to_path_buf(),
        exports: Vec::new(),
    }
}

fn agent(dir: &TempDir, api: FakeApi, state: State) -> Agent<FakeApi> {
    let stack = Url::parse("https://stack.example.com/").unwrap();
    StateStore::new(dir.path()).save(&state).unwrap();
    Agent::new(&config(dir, &stack), api).unwrap()
}

fn saved(dir: &TempDir) -> State {
    StateStore::new(dir.path()).load().unwrap()
}

fn write_config(dir: &TempDir, stack: &Url, exports: &str) -> PathBuf {
    let path = dir.path().join("config.toml");
    let body = format!(
        "stack_url = {:?}\nname = \"alpha\"\nstate_dir = {:?}\n{exports}",
        stack.as_str(),
        dir.path().display().to_string()
    );
    fs::write(&path, body).unwrap();
    path
}

#[tokio::test]
async fn a_new_agent_registers_keeps_its_code_and_waits() {
    let dir = tempfile::tempdir().unwrap();
    let api = FakeApi::default();
    let mut agent = agent(&dir, api.clone(), State::default());

    assert_eq!(agent.cycle().await.unwrap(), Step::Wait(CLAIM_POLL));

    assert_eq!(*api.0.registered.lock().unwrap(), vec![None]);
    assert_eq!(saved(&dir).claim_code.as_deref(), Some("delta"));
}

#[tokio::test]
async fn a_registration_offers_the_exports() {
    let dir = tempfile::tempdir().unwrap();
    let api = FakeApi::default();
    let mut agent = agent(&dir, api.clone(), State::default());
    agent.exports = vec![Export {
        name: "epsilon".into(),
        path: "/srv/epsilon".into(),
    }];

    agent.cycle().await.unwrap();

    assert_eq!(
        *api.0.registered_exports.lock().unwrap(),
        vec![agent.exports.clone()]
    );
}

#[tokio::test]
async fn a_waiting_claim_is_polled_every_ten_seconds() {
    let dir = tempfile::tempdir().unwrap();
    let state = State {
        claim_code: Some("delta".into()),
        ..State::default()
    };
    let mut agent = agent(&dir, FakeApi::default(), state);

    assert_eq!(agent.cycle().await.unwrap(), Step::Wait(CLAIM_POLL));
    assert_eq!(CLAIM_POLL, Duration::from_secs(10));
}

#[tokio::test]
async fn a_granted_claim_stores_the_id_and_the_credential() {
    let dir = tempfile::tempdir().unwrap();
    let api = FakeApi::default();
    api.0.claims.lock().unwrap().extend([
        Claim::Waiting,
        Claim::Granted {
            agent_id: "gamma".into(),
            credential: credential("k1:1:1", 3600),
        },
    ]);
    let state = State {
        claim_code: Some("delta".into()),
        ..State::default()
    };
    let mut agent = agent(&dir, api, state);

    assert_eq!(agent.cycle().await.unwrap(), Step::Wait(CLAIM_POLL));
    assert_eq!(agent.cycle().await.unwrap(), Step::Wait(Duration::ZERO));

    let state = saved(&dir);
    assert_eq!(state.agent_id.as_deref(), Some("gamma"));
    assert!(state.claim_code.is_none());
    assert!(state.credential.is_some());
}

#[tokio::test]
async fn enrollment_announces_the_exports_before_the_socket() {
    let dir = tempfile::tempdir().unwrap();
    let api = FakeApi::default();
    let mut agent = agent(&dir, api.clone(), enrolled(3600));

    assert_eq!(agent.cycle().await.unwrap(), Step::Connect);

    assert_eq!(api.0.announced.lock().unwrap().len(), 1);
    agent.cycle().await.unwrap();
    assert_eq!(api.0.announced.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn a_decision_still_to_come_is_asked_about_sooner() {
    let dir = tempfile::tempdir().unwrap();
    let reported = |state: &str, bound_to: Option<&str>| Reported {
        name: "epsilon".into(),
        host_path: "/srv/epsilon".into(),
        state: state.into(),
        bound_to: bound_to.map(str::to_owned),
    };
    let mut agent = agent(&dir, FakeApi::default(), enrolled(3600));

    assert_eq!(agent.announce_every(), ANNOUNCE_EVERY);
    agent.state.exports = vec![reported("pending", None)];
    assert_eq!(agent.announce_every(), ANNOUNCE_UNSETTLED);
    agent.state.exports = vec![reported("approved", None)];
    assert_eq!(agent.announce_every(), ANNOUNCE_UNSETTLED);
    agent.state.exports = vec![reported("approved", Some("zeta"))];
    assert_eq!(agent.announce_every(), ANNOUNCE_EVERY);
    agent.state.exports = vec![reported("refused", None)];
    assert_eq!(agent.announce_every(), ANNOUNCE_EVERY);
}

#[tokio::test]
async fn where_the_exports_stand_is_kept_for_status() {
    let dir = tempfile::tempdir().unwrap();
    let api = FakeApi::default();
    let reported = vec![Reported {
        name: "epsilon".into(),
        host_path: "/srv/epsilon".into(),
        state: "approved".into(),
        bound_to: None,
    }];
    api.0
        .announcements
        .lock()
        .unwrap()
        .push_back(Ok(Signed::Accepted(reported.clone())));
    let mut agent = agent(&dir, api, enrolled(3600));

    agent.cycle().await.unwrap();

    assert_eq!(saved(&dir).exports, reported);
}

#[tokio::test]
async fn a_refused_claim_registers_again_under_the_same_id() {
    let dir = tempfile::tempdir().unwrap();
    let api = FakeApi::default();
    api.0.claims.lock().unwrap().push_back(Claim::Refused);
    let state = State {
        agent_id: Some("gamma".into()),
        claim_code: Some("old".into()),
        ..State::default()
    };
    let mut agent = agent(&dir, api.clone(), state);

    assert_eq!(agent.cycle().await.unwrap(), Step::Wait(Duration::ZERO));
    assert!(saved(&dir).claim_code.is_none());

    assert_eq!(agent.cycle().await.unwrap(), Step::Wait(CLAIM_POLL));
    assert_eq!(
        *api.0.registered.lock().unwrap(),
        vec![Some("gamma".to_owned())]
    );
    assert_eq!(saved(&dir).claim_code.as_deref(), Some("delta"));
}

#[tokio::test]
async fn a_credential_due_for_refresh_is_swapped_and_the_old_one_never_signs_again() {
    let dir = tempfile::tempdir().unwrap();
    let api = FakeApi::default();
    api.0
        .refreshes
        .lock()
        .unwrap()
        .push_back(Ok(Signed::Accepted(credential("k1:2:2", 3600))));
    let mut agent = agent(&dir, api.clone(), enrolled(60));

    assert_eq!(agent.cycle().await.unwrap(), Step::Connect);

    assert_eq!(
        *api.0.signed_with.lock().unwrap(),
        vec!["k1:1:1".to_owned(), "k1:2:2".to_owned()]
    );
    assert_eq!(saved(&dir).credential.unwrap().header, "k1:2:2");
    assert_eq!(agent.signer().unwrap().credential.header, "k1:2:2");
}

#[tokio::test]
async fn a_credential_with_time_left_is_not_refreshed() {
    let dir = tempfile::tempdir().unwrap();
    let api = FakeApi::default();
    let mut agent = agent(&dir, api.clone(), enrolled(3600));

    agent.cycle().await.unwrap();

    assert_eq!(
        *api.0.signed_with.lock().unwrap(),
        vec!["k1:1:1".to_owned()]
    );
}

#[tokio::test]
async fn a_refused_refresh_drops_the_credential_and_keeps_the_id() {
    let dir = tempfile::tempdir().unwrap();
    let api = FakeApi::default();
    let mut agent = agent(&dir, api.clone(), enrolled(60));

    assert_eq!(agent.cycle().await.unwrap(), Step::Wait(Duration::ZERO));

    let state = saved(&dir);
    assert!(state.credential.is_none());
    assert_eq!(state.agent_id.as_deref(), Some("gamma"));

    agent.cycle().await.unwrap();
    assert_eq!(
        *api.0.registered.lock().unwrap(),
        vec![Some("gamma".to_owned())]
    );
}

#[tokio::test]
async fn a_refused_announcement_drops_the_credential() {
    let dir = tempfile::tempdir().unwrap();
    let api = FakeApi::default();
    api.0
        .announcements
        .lock()
        .unwrap()
        .push_back(Ok(Signed::Refused));
    let mut agent = agent(&dir, api, enrolled(3600));

    assert_eq!(agent.cycle().await.unwrap(), Step::Wait(Duration::ZERO));

    assert!(saved(&dir).credential.is_none());
}

#[tokio::test]
async fn a_stack_that_cannot_answer_costs_no_credential() {
    let dir = tempfile::tempdir().unwrap();
    let api = FakeApi::default();
    api.0
        .announcements
        .lock()
        .unwrap()
        .push_back(Err(AgentError::Api("answered 503".into())));
    api.0
        .refreshes
        .lock()
        .unwrap()
        .push_back(Err(AgentError::Api("answered 503".into())));
    let mut announcing = agent(&dir, api.clone(), enrolled(3600));

    assert!(announcing.cycle().await.is_err());
    assert!(saved(&dir).credential.is_some());

    let mut due = agent(&dir, api, enrolled(60));
    assert!(due.cycle().await.is_err());
    assert_eq!(saved(&dir).credential.unwrap().header, "k1:1:1");
    assert_eq!(due.signer().unwrap().credential.header, "k1:1:1");
}

#[tokio::test]
async fn a_changed_export_is_announced_again() {
    let dir = tempfile::tempdir().unwrap();
    let tree = dir.path().join("epsilon");
    fs::create_dir(&tree).unwrap();
    let stack = Url::parse("https://stack.example.com/").unwrap();
    let path = write_config(&dir, &stack, "");
    let api = FakeApi::default();
    let mut agent = agent(&dir, api.clone(), enrolled(3600));
    agent.cycle().await.unwrap();

    assert!(!agent.reload(&path));

    let export = format!(
        "[[project]]\nname = \"epsilon\"\npath = {:?}\n",
        tree.display().to_string()
    );
    write_config(&dir, &stack, &export);
    assert!(agent.reload(&path));
    agent.cycle().await.unwrap();

    let announced = api.0.announced.lock().unwrap();
    assert_eq!(announced.len(), 2);
    assert_eq!(announced[1][0].name, "epsilon");
}

#[tokio::test]
async fn a_config_gone_bad_keeps_the_exports_already_loaded() {
    let dir = tempfile::tempdir().unwrap();
    let stack = Url::parse("https://stack.example.com/").unwrap();
    let path = write_config(&dir, &stack, "");
    let mut agent = agent(&dir, FakeApi::default(), enrolled(3600));
    agent.exports = vec![Export {
        name: "epsilon".into(),
        path: "/srv/epsilon".into(),
    }];
    fs::write(&path, "not toml at all").unwrap();

    assert!(!agent.reload(&path));

    assert_eq!(agent.exports.len(), 1);
    assert!(agent.config_broken);
}

type Socket = WebSocketStream<tokio::net::TcpStream>;

#[allow(
    clippy::result_large_err,
    reason = "the library fixes the callback's error type"
)]
async fn accepted(listener: &TcpListener) -> (Socket, String) {
    let (stream, _) = listener.accept().await.unwrap();
    let mut header = String::new();
    let mut socket = accept_hdr_async(stream, |request: &Request, response: Response| {
        header = request.headers()["X-Credential"]
            .to_str()
            .unwrap()
            .to_owned();
        Ok(response)
    })
    .await
    .unwrap();
    socket.next().await.unwrap().unwrap();
    let answer = json!({"type": "hello", "protocol": 1}).to_string();
    socket.send(Message::text(answer)).await.unwrap();
    (socket, header)
}

async fn listening() -> (TcpListener, Url) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    (listener, Url::parse(&format!("http://{address}/")).unwrap())
}

#[tokio::test]
async fn a_socket_the_stack_closes_is_dialled_again_and_announced_again() {
    let dir = tempfile::tempdir().unwrap();
    let (listener, stack) = listening().await;
    let path = write_config(&dir, &stack, "");
    StateStore::new(dir.path()).save(&enrolled(3600)).unwrap();
    let api = FakeApi::default();
    let mut agent = Agent::new(&config(&dir, &stack), api.clone()).unwrap();

    let serving = async {
        let (mut first, _) = accepted(&listener).await;
        first
            .close(Some(CloseFrame {
                code: CloseCode::Normal,
                reason: "expired".into(),
            }))
            .await
            .unwrap();
        accepted(&listener).await
    };
    let (_second, header) = tokio::select! {
        served = timeout(WAIT, serving) => served.unwrap(),
        () = supervise(&mut agent, &path) => unreachable!(),
    };

    assert_eq!(header, "k1:1:1");
    assert_eq!(api.0.announced.lock().unwrap().len(), 2);
}

#[tokio::test]
async fn a_refresh_closes_the_socket_before_the_new_generation_signs() {
    let dir = tempfile::tempdir().unwrap();
    let (listener, stack) = listening().await;
    let path = write_config(&dir, &stack, "");
    StateStore::new(dir.path())
        .save(&enrolled(15 * 60 + 2))
        .unwrap();
    let api = FakeApi::default();
    api.0
        .refreshes
        .lock()
        .unwrap()
        .push_back(Ok(Signed::Accepted(credential("k1:2:2", 3600))));
    let mut agent = Agent::new(&config(&dir, &stack), api.clone()).unwrap();

    let serving = async {
        let (mut first, old) = accepted(&listener).await;
        // The agent's close is what ends this read, not the server's.
        while let Some(Ok(message)) = first.next().await {
            if message.is_close() {
                break;
            }
        }
        let (second, new) = accepted(&listener).await;
        (second, old, new)
    };
    let (_second, old, new) = tokio::select! {
        served = timeout(WAIT, serving) => served.unwrap(),
        () = supervise(&mut agent, &path) => unreachable!(),
    };

    assert_eq!(old, "k1:1:1");
    assert_eq!(new, "k1:2:2");
    assert_eq!(
        *api.0.signed_with.lock().unwrap(),
        vec![
            "k1:1:1".to_owned(),
            "k1:1:1".to_owned(),
            "k1:2:2".to_owned()
        ]
    );
}
