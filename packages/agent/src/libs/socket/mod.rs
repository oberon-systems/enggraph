use std::time::{Duration, SystemTime, UNIX_EPOCH};

use futures_util::{SinkExt, StreamExt};
use serde::Deserialize;
use serde_json::json;
use tokio::net::TcpStream;
use tokio::time::{interval_at, timeout, Instant, Interval, MissedTickBehavior};
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::handshake::client::Request;
use tokio_tungstenite::tungstenite::http::HeaderValue;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::{connect_async, MaybeTlsStream, WebSocketStream};
use url::Url;

use crate::libs::api::{agents_url, sign, unix_now};
use crate::libs::enroll::Signer;
use crate::libs::error::AgentError;
use crate::libs::exports::Export;
use crate::libs::protocol::{self, request_of, Limits};

const PROTOCOL: u32 = 1;
const CONNECT_WAIT: Duration = Duration::from_secs(10);
const HELLO_WAIT: Duration = Duration::from_secs(30);
const SEND_WAIT: Duration = Duration::from_secs(10);
const CLOSE_WAIT: Duration = Duration::from_secs(2);
const SHORTEST_PAUSE_MS: u64 = 1_000;
const LONGEST_PAUSE_MS: u64 = 60_000;
// A ping frame of the protocol would not do: uvicorn answers it without the stack seeing it.
const PING: &str = r#"{"type":"ping"}"#;

/// How often to ping, and how long a quiet stack is believed to be there.
#[derive(Debug, Clone, Copy)]
pub struct Timing {
    pub ping: Duration,
    pub silence: Duration,
}

impl Default for Timing {
    fn default() -> Self {
        Self {
            ping: Duration::from_secs(20),
            silence: Duration::from_secs(60),
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum Event {
    Heard,
    PingDue,
    Request(protocol::Request),
    Ended(String),
}

#[derive(Deserialize)]
struct Frame {
    #[serde(rename = "type")]
    kind: String,
    protocol: Option<u32>,
    limits: Option<Limits>,
}

pub struct Connection {
    stream: WebSocketStream<MaybeTlsStream<TcpStream>>,
    ticker: Interval,
    heard: Instant,
    silence: Duration,
    limits: Limits,
}

fn failed(err: impl std::fmt::Display) -> AgentError {
    AgentError::Socket(err.to_string())
}

fn request(stack: &Url, signer: &Signer) -> Result<Request, AgentError> {
    let target = agents_url(stack, &format!("{}/socket", signer.agent_id))?;
    // http becomes ws and https becomes wss.
    let rest = target
        .as_str()
        .strip_prefix("http")
        .ok_or_else(|| failed("the stack URL is not http or https"))?;
    let timestamp = unix_now().to_string();
    let signature = sign(
        &signer.credential.key,
        &timestamp,
        "GET",
        target.path(),
        &[],
    )?;
    let mut request = format!("ws{rest}").into_client_request().map_err(failed)?;
    for (name, value) in [
        ("X-Credential", signer.credential.header.as_str()),
        ("X-Timestamp", timestamp.as_str()),
        ("X-Signature", signature.as_str()),
    ] {
        let value = HeaderValue::from_str(value).map_err(|_| failed("a header is not ASCII"))?;
        request.headers_mut().insert(name, value);
    }
    Ok(request)
}

impl Connection {
    pub async fn open(
        stack: &Url,
        signer: &Signer,
        exports: &[Export],
    ) -> Result<Self, AgentError> {
        Self::open_with(stack, signer, exports, Timing::default()).await
    }

    /// Dials, signed as a GET, and returns once the stack has answered the hello.
    pub async fn open_with(
        stack: &Url,
        signer: &Signer,
        exports: &[Export],
        timing: Timing,
    ) -> Result<Self, AgentError> {
        let (mut stream, _) = timeout(CONNECT_WAIT, connect_async(request(stack, signer)?))
            .await
            .map_err(|_| failed("connecting timed out"))?
            .map_err(failed)?;
        let hello = json!({
            "type": "hello",
            "protocol": PROTOCOL,
            "version": env!("CARGO_PKG_VERSION"),
            "exports": exports,
        });
        stream
            .send(Message::text(hello.to_string()))
            .await
            .map_err(failed)?;
        let limits = timeout(HELLO_WAIT, answered(&mut stream))
            .await
            .map_err(|_| failed("the stack did not answer the hello"))??;
        let mut ticker = interval_at(Instant::now() + timing.ping, timing.ping);
        ticker.set_missed_tick_behavior(MissedTickBehavior::Delay);
        Ok(Self {
            stream,
            ticker,
            heard: Instant::now(),
            silence: timing.silence,
            limits,
        })
    }

    pub fn limits(&self) -> Limits {
        self.limits.clone()
    }

    /// Safe to drop half way: nothing is sent from here, so a frame cannot be cut.
    pub async fn next_event(&mut self) -> Event {
        tokio::select! {
            frame = self.stream.next() => match frame {
                None => Event::Ended("the stack hung up".into()),
                Some(Err(err)) => Event::Ended(err.to_string()),
                Some(Ok(Message::Close(frame))) => Event::Ended(match frame {
                    Some(frame) => format!("the stack closed with {}", u16::from(frame.code)),
                    None => "the stack closed".into(),
                }),
                Some(Ok(message)) => {
                    self.heard = Instant::now();
                    match message.to_text().ok().and_then(request_of) {
                        Some(request) => Event::Request(request),
                        None => Event::Heard,
                    }
                }
            },
            _ = self.ticker.tick() => Event::PingDue,
        }
    }

    pub async fn ping(&mut self) -> Result<(), AgentError> {
        if self.heard.elapsed() > self.silence {
            return Err(failed("the stack went silent"));
        }
        timeout(SEND_WAIT, self.stream.send(Message::text(PING)))
            .await
            .map_err(|_| failed("sending a ping timed out"))?
            .map_err(failed)
    }

    pub async fn send(&mut self, message: Message) -> Result<(), AgentError> {
        timeout(SEND_WAIT, self.stream.send(message))
            .await
            .map_err(|_| failed("sending an answer timed out"))?
            .map_err(failed)
    }

    pub async fn close(&mut self) {
        let _ = timeout(CLOSE_WAIT, self.stream.close(None)).await;
    }
}

async fn answered(
    stream: &mut WebSocketStream<MaybeTlsStream<TcpStream>>,
) -> Result<Limits, AgentError> {
    loop {
        let message = stream
            .next()
            .await
            .ok_or_else(|| failed("the stack hung up before the hello"))?
            .map_err(failed)?;
        match message {
            Message::Text(text) => {
                let frame: Frame = serde_json::from_str(&text)
                    .map_err(|_| failed("the hello is not what was expected"))?;
                if frame.kind != "hello" || frame.protocol != Some(PROTOCOL) {
                    return Err(failed("the stack speaks another protocol"));
                }
                return Ok(frame.limits.unwrap_or_default());
            }
            Message::Close(_) => return Err(failed("the stack closed before the hello")),
            _ => {}
        }
    }
}

/// How long to wait before dialling again: 1 s, doubling to 60 s, with jitter on top.
#[derive(Debug, Default)]
pub struct Backoff {
    attempt: u32,
}

impl Backoff {
    pub fn pause(&mut self) -> Duration {
        let noise = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0, |elapsed| elapsed.subsec_nanos());
        let pause = delay(self.attempt, noise);
        self.attempt = self.attempt.saturating_add(1);
        pause
    }

    pub fn reset(&mut self) {
        self.attempt = 0;
    }
}

fn delay(attempt: u32, noise: u32) -> Duration {
    let base = SHORTEST_PAUSE_MS
        .saturating_mul(1 << attempt.min(16))
        .min(LONGEST_PAUSE_MS);
    let jitter = u64::from(noise) % (base / 2 + 1);
    Duration::from_millis((base + jitter).min(LONGEST_PAUSE_MS))
}

#[cfg(test)]
mod tests;
