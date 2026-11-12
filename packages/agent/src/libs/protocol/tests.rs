use serde::Deserialize;
use serde_json::{json, Value};

use super::{changed, data, failure, request_of, resync, success, Limits, Request};

const EXAMPLES: &str = include_str!("../../../../core/tests/data/agent_protocol_examples.json");

#[derive(Deserialize)]
struct Examples {
    requests: Vec<RequestCase>,
    answers: Vec<AnswerCase>,
    data: Vec<DataCase>,
    events: Vec<EventCase>,
}

#[derive(Deserialize)]
struct RequestCase {
    text: String,
    id: u32,
    op: String,
    args: Value,
}

#[derive(Deserialize)]
struct AnswerCase {
    id: u32,
    ok: bool,
    #[serde(default)]
    result: Value,
    #[serde(default)]
    more: bool,
    #[serde(default)]
    code: String,
    #[serde(default)]
    message: String,
    frame: Value,
}

#[derive(Deserialize)]
struct DataCase {
    id: u32,
    last: bool,
    payload_hex: String,
    frame_hex: String,
}

#[derive(Deserialize)]
struct EventCase {
    export: String,
    paths: Option<Vec<String>>,
    frame: Value,
}

fn examples() -> Examples {
    serde_json::from_str(EXAMPLES).unwrap()
}

#[test]
fn a_request_reads_as_the_shared_examples_say() {
    for case in examples().requests {
        assert_eq!(
            request_of(&case.text),
            Some(Request {
                id: case.id,
                op: case.op,
                args: case.args,
            })
        );
    }
}

#[test]
fn a_pong_is_not_a_request() {
    assert_eq!(request_of(r#"{"type":"pong"}"#), None);
    assert_eq!(request_of("not json"), None);
}

#[test]
fn an_answer_is_spelled_as_the_shared_examples_say() {
    for case in examples().answers {
        let text = if case.ok {
            success(case.id, &case.result, case.more)
        } else {
            failure(case.id, &case.code, &case.message)
        };
        assert_eq!(serde_json::from_str::<Value>(&text).unwrap(), case.frame);
    }
}

#[test]
fn a_data_frame_is_spelled_as_the_shared_examples_say() {
    for case in examples().data {
        let payload = hex::decode(&case.payload_hex).unwrap();
        assert_eq!(
            hex::encode(data(case.id, case.last, &payload)),
            case.frame_hex
        );
    }
}

#[test]
fn an_event_is_spelled_as_the_shared_examples_say() {
    for case in examples().events {
        let text = match &case.paths {
            Some(paths) => changed(&case.export, paths),
            None => resync(&case.export),
        };
        assert_eq!(serde_json::from_str::<Value>(&text).unwrap(), case.frame);
    }
}

#[test]
fn a_request_without_args_has_none() {
    let request = request_of(r#"{"id":1,"op":"stat"}"#).unwrap();
    assert_eq!(request.args, json!(null));
}

#[test]
fn a_limit_no_stack_means_is_held_to_what_an_agent_reads() {
    let sent = Limits {
        max_file_bytes: u64::MAX,
        skip_dirs: Vec::new(),
    };
    assert!(sent.bounded().max_file_bytes < u64::MAX / 2);
    assert_eq!(Limits::default().bounded(), Limits::default());
}
