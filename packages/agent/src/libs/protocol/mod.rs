use serde::Deserialize;
use serde_json::{json, Value};

const LAST: u8 = 1;
const ID_BYTES: usize = 4;

/// What the stack told this agent in its hello: limits are the stack's to set.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct Limits {
    pub max_file_bytes: u64,
    #[serde(default)]
    pub skip_dirs: Vec<String>,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            max_file_bytes: 1_000_000,
            skip_dirs: Vec::new(),
        }
    }
}

/// One operation the stack asks for. Answers carry its id, so requests interleave.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct Request {
    pub id: u32,
    pub op: String,
    #[serde(default)]
    pub args: Value,
}

/// Anything else that arrives as a text frame, a pong for one, is not a request.
pub fn request_of(text: &str) -> Option<Request> {
    serde_json::from_str(text).ok()
}

pub fn success(id: u32, result: &Value, more: bool) -> String {
    if more {
        json!({"id": id, "ok": true, "result": result, "more": true}).to_string()
    } else {
        json!({"id": id, "ok": true, "result": result}).to_string()
    }
}

pub fn failure(id: u32, code: &str, message: &str) -> String {
    json!({"id": id, "ok": false, "error": {"code": code, "message": message}}).to_string()
}

/// A binary frame: 4 bytes of request id, big-endian, 1 byte of flags, the payload.
pub fn data(id: u32, last: bool, payload: &[u8]) -> Vec<u8> {
    let mut frame = Vec::with_capacity(ID_BYTES + 1 + payload.len());
    frame.extend_from_slice(&id.to_be_bytes());
    frame.push(if last { LAST } else { 0 });
    frame.extend_from_slice(payload);
    frame
}

pub fn changed(export: &str, paths: &[String]) -> String {
    json!({"type": "changed", "export": export, "paths": paths}).to_string()
}

pub fn resync(export: &str) -> String {
    json!({"type": "resync", "export": export}).to_string()
}

#[cfg(test)]
mod tests;
