use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio::net::{TcpListener, TcpStream};
use tokio::time::timeout;
use tokio_tungstenite::tungstenite::handshake::server::{Request, Response};
use tokio_tungstenite::tungstenite::http;
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;
use tokio_tungstenite::tungstenite::protocol::CloseFrame;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::{accept_async, accept_hdr_async, WebSocketStream};
use url::Url;

use super::{delay, Backoff, Connection, Event, Timing, PING};
use crate::libs::api::sign;
use crate::libs::enroll::Signer;
use crate::libs::error::AgentError;
use crate::libs::exports::Export;
use crate::libs::protocol::{self, Limits};
use crate::libs::state::Credential;

const WAIT: Duration = Duration::from_secs(5);

type Socket = WebSocketStream<TcpStream>;

fn signer() -> Signer {
    Signer {
        agent_id: "alpha".into(),
        credential: Credential {
            header: "k1:1:1893456000".into(),
            expires_at: 1_893_456_000,
            key: "beta".into(),
        },
    }
}

fn exports() -> Vec<Export> {
    vec![Export {
        name: "gamma".into(),
        path: "/srv/gamma".into(),
    }]
}

async fn listening() -> (TcpListener, Url) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    (listener, Url::parse(&format!("http://{address}/")).unwrap())
}

async fn read_json(socket: &mut Socket) -> Value {
    let message = timeout(WAIT, socket.next())
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    serde_json::from_str(message.to_text().unwrap()).unwrap()
}

async fn answer_hello(socket: &mut Socket) -> Value {
    let hello = read_json(socket).await;
    let answer = json!({"type": "hello", "protocol": 1}).to_string();
    socket.send(Message::text(answer)).await.unwrap();
    hello
}

async fn greeted(listener: TcpListener) -> Socket {
    let (stream, _) = listener.accept().await.unwrap();
    let mut socket = accept_async(stream).await.unwrap();
    answer_hello(&mut socket).await;
    socket
}

#[tokio::test]
#[allow(
    clippy::result_large_err,
    reason = "the library fixes the callback's error type"
)]
async fn the_upgrade_is_signed_as_a_get_of_the_gateway_path() {
    let (listener, stack) = listening().await;
    let dialling =
        tokio::spawn(async move { Connection::open(&stack, &signer(), &exports()).await });

    let (stream, _) = listener.accept().await.unwrap();
    let mut seen = Vec::new();
    let mut socket = accept_hdr_async(stream, |request: &Request, response: Response| {
        seen.push(request.uri().path().to_owned());
        for name in ["X-Credential", "X-Timestamp", "X-Signature"] {
            let value = request.headers().get(name).unwrap().to_str().unwrap();
            seen.push(value.to_owned());
        }
        Ok(response)
    })
    .await
    .unwrap();
    answer_hello(&mut socket).await;
    dialling.await.unwrap().unwrap();

    let [path, header, timestamp, signature] = seen.try_into().unwrap();
    assert_eq!(path, "/agent/agents/alpha/socket");
    assert_eq!(header, "k1:1:1893456000");
    assert_eq!(
        signature,
        sign("beta", &timestamp, "GET", &path, &[]).unwrap()
    );
}

#[tokio::test]
async fn the_hello_names_the_protocol_the_version_and_the_exports() {
    let (listener, stack) = listening().await;
    let dialling =
        tokio::spawn(async move { Connection::open(&stack, &signer(), &exports()).await });

    let (stream, _) = listener.accept().await.unwrap();
    let mut socket = accept_async(stream).await.unwrap();
    let hello = answer_hello(&mut socket).await;
    dialling.await.unwrap().unwrap();

    assert_eq!(
        hello,
        json!({
            "type": "hello",
            "protocol": 1,
            "version": env!("CARGO_PKG_VERSION"),
            "exports": [{"name": "gamma", "path": "/srv/gamma"}],
        })
    );
}

#[tokio::test]
#[allow(
    clippy::result_large_err,
    reason = "the library fixes the callback's error type"
)]
async fn a_refused_upgrade_is_an_error() {
    let (listener, stack) = listening().await;
    let dialling = tokio::spawn(async move { Connection::open(&stack, &signer(), &[]).await });

    let (stream, _) = listener.accept().await.unwrap();
    let refused = accept_hdr_async(stream, |_: &Request, _: Response| {
        Err(http::Response::builder().status(403).body(None).unwrap())
    })
    .await;

    assert!(refused.is_err());
    assert!(matches!(
        dialling.await.unwrap(),
        Err(AgentError::Socket(message)) if message.contains("403")
    ));
}

#[tokio::test]
async fn a_stack_on_another_protocol_is_refused() {
    let (listener, stack) = listening().await;
    let dialling = tokio::spawn(async move { Connection::open(&stack, &signer(), &[]).await });

    let (stream, _) = listener.accept().await.unwrap();
    let mut socket = accept_async(stream).await.unwrap();
    read_json(&mut socket).await;
    let answer = json!({"type": "hello", "protocol": 2}).to_string();
    socket.send(Message::text(answer)).await.unwrap();

    assert!(matches!(
        dialling.await.unwrap(),
        Err(AgentError::Socket(message)) if message.contains("another protocol")
    ));
}

#[tokio::test]
async fn the_hello_answer_sets_the_limits() {
    let (listener, stack) = listening().await;
    let serving = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut socket = accept_async(stream).await.unwrap();
        read_json(&mut socket).await;
        let answer = json!({
            "type": "hello",
            "protocol": 1,
            "limits": {"max_file_bytes": 5, "skip_dirs": [".git"]},
        });
        socket
            .send(Message::text(answer.to_string()))
            .await
            .unwrap();
        socket
    });
    let connection = Connection::open(&stack, &signer(), &[]).await.unwrap();
    let _socket = serving.await.unwrap();

    assert_eq!(connection.limits().max_file_bytes, 5);
    assert_eq!(connection.limits().skip_dirs, [".git"]);
}

#[tokio::test]
async fn a_hello_answer_without_limits_leaves_the_defaults() {
    let (listener, stack) = listening().await;
    let serving = tokio::spawn(greeted(listener));
    let connection = Connection::open(&stack, &signer(), &[]).await.unwrap();
    let _socket = serving.await.unwrap();

    assert_eq!(connection.limits(), Limits::default());
}

#[tokio::test]
async fn a_request_arrives_as_an_event_and_an_answer_goes_back() {
    let (listener, stack) = listening().await;
    let serving = tokio::spawn(greeted(listener));
    let mut connection = Connection::open(&stack, &signer(), &[]).await.unwrap();
    let mut socket = serving.await.unwrap();

    let asked = json!({"id": 7, "op": "stat", "args": {"export": "gamma"}}).to_string();
    socket.send(Message::text(asked)).await.unwrap();

    let event = timeout(WAIT, connection.next_event()).await.unwrap();
    assert_eq!(
        event,
        Event::Request(protocol::Request {
            id: 7,
            op: "stat".into(),
            args: json!({"export": "gamma"}),
        })
    );

    connection.send(Message::binary(vec![1, 2])).await.unwrap();
    let message = timeout(WAIT, socket.next())
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    assert_eq!(message.into_data().to_vec(), vec![1, 2]);
}

#[tokio::test]
async fn a_tls_stack_gets_as_far_as_the_handshake_rather_than_a_panic() {
    let (listener, stack) = listening().await;
    let port = stack.port().unwrap();
    let tls = Url::parse(&format!("https://127.0.0.1:{port}/")).unwrap();
    let dialling = tokio::spawn(async move { Connection::open(&tls, &signer(), &[]).await });

    let (stream, _) = listener.accept().await.unwrap();
    drop(stream);

    assert!(matches!(
        dialling.await.unwrap(),
        Err(AgentError::Socket(_))
    ));
}

#[tokio::test]
async fn a_ping_is_a_text_frame_the_stack_answers() {
    let (listener, stack) = listening().await;
    let serving = tokio::spawn(greeted(listener));
    let mut connection = Connection::open(&stack, &signer(), &[]).await.unwrap();
    let mut socket = serving.await.unwrap();

    connection.ping().await.unwrap();

    assert_eq!(read_json(&mut socket).await, json!({"type": "ping"}));
    let pong = json!({"type": "pong"}).to_string();
    socket.send(Message::text(pong)).await.unwrap();
    assert_eq!(connection.next_event().await, Event::Heard);
    assert_eq!(PING, r#"{"type":"ping"}"#);
}

#[tokio::test]
async fn a_ping_falls_due_on_the_timer() {
    let (listener, stack) = listening().await;
    let serving = tokio::spawn(greeted(listener));
    let timing = Timing {
        ping: Duration::from_millis(20),
        silence: Duration::from_secs(60),
    };
    let mut connection = Connection::open_with(&stack, &signer(), &[], timing)
        .await
        .unwrap();
    let _socket = serving.await.unwrap();

    let event = timeout(WAIT, connection.next_event()).await.unwrap();

    assert_eq!(event, Event::PingDue);
}

#[tokio::test]
async fn a_silent_stack_is_given_up_on() {
    let (listener, stack) = listening().await;
    let serving = tokio::spawn(greeted(listener));
    let timing = Timing {
        ping: Duration::from_secs(60),
        silence: Duration::from_millis(10),
    };
    let mut connection = Connection::open_with(&stack, &signer(), &[], timing)
        .await
        .unwrap();
    let _socket = serving.await.unwrap();

    tokio::time::sleep(Duration::from_millis(50)).await;

    assert!(matches!(
        connection.ping().await,
        Err(AgentError::Socket(message)) if message.contains("silent")
    ));
}

#[tokio::test]
async fn a_close_from_the_stack_ends_the_connection_with_its_code() {
    let (listener, stack) = listening().await;
    let serving = tokio::spawn(greeted(listener));
    let mut connection = Connection::open(&stack, &signer(), &[]).await.unwrap();
    let mut socket = serving.await.unwrap();

    socket
        .close(Some(CloseFrame {
            code: CloseCode::Normal,
            reason: "expired".into(),
        }))
        .await
        .unwrap();

    let event = timeout(WAIT, connection.next_event()).await.unwrap();
    assert_eq!(event, Event::Ended("the stack closed with 1000".into()));
}

#[tokio::test]
async fn a_dropped_stream_ends_the_connection() {
    let (listener, stack) = listening().await;
    let serving = tokio::spawn(greeted(listener));
    let mut connection = Connection::open(&stack, &signer(), &[]).await.unwrap();

    drop(serving.await.unwrap());

    let event = timeout(WAIT, connection.next_event()).await.unwrap();
    assert!(matches!(event, Event::Ended(_)));
}

#[test]
fn the_pause_doubles_from_one_second_to_sixty() {
    let seconds = |attempt| delay(attempt, 0).as_secs();

    assert_eq!(seconds(0), 1);
    assert_eq!(seconds(1), 2);
    assert_eq!(seconds(5), 32);
    assert_eq!(seconds(6), 60);
    assert_eq!(seconds(u32::MAX), 60);
}

#[test]
fn the_jitter_stays_inside_the_limits() {
    for noise in [0, 1, 499, 500, 501, u32::MAX] {
        let first = delay(0, noise);
        assert!(first >= Duration::from_secs(1));
        assert!(first <= Duration::from_millis(1_500));
        assert!(delay(20, noise) == Duration::from_secs(60));
    }
}

#[test]
fn a_reset_starts_the_pause_over() {
    let mut backoff = Backoff::default();
    backoff.pause();
    backoff.pause();
    assert!(backoff.pause() >= Duration::from_secs(4));

    backoff.reset();

    assert!(backoff.pause() < Duration::from_secs(2));
}
